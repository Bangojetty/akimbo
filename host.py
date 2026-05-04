import argparse
import asyncio
import ctypes
import os
import struct
import sys
import threading
import time

import websockets
from PySide6 import QtCore, QtGui, QtWidgets

from protocol import decode_image, decode_position

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CURSORS_DIR = os.path.join(SCRIPT_DIR, "cursors")
CURSOR_BASE_PX = 48
CURSOR_HOVER_SCALE = 1.10
CURSOR_CLICK_SCALE = 0.85

user32 = ctypes.windll.user32
user32.SetProcessDPIAware()

GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080

STALE_MS = 500
TICK_MS = 8  # ~120 fps redraw cadence


class State:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.x = 0.0
        self.y = 0.0
        self.last_update_ms = 0
        self.default_cursor = True
        self.rmb = False
        self.image_jpeg: bytes | None = None
        self.image_seq = 0
        self.pos_x_norm = 0.7
        self.pos_y_norm = 0.0

    def update(self, x: float, y: float, default_cursor: bool, rmb: bool) -> None:
        with self.lock:
            self.x = x
            self.y = y
            self.default_cursor = default_cursor
            self.rmb = rmb
            self.last_update_ms = int(time.time() * 1000)

    def snapshot(self) -> tuple[float, float, int, bool, bool]:
        with self.lock:
            return self.x, self.y, self.last_update_ms, self.default_cursor, self.rmb

    def update_image(self, jpeg: bytes) -> None:
        with self.lock:
            self.image_jpeg = jpeg
            self.image_seq += 1

    def snapshot_image(self) -> tuple[bytes | None, int]:
        with self.lock:
            return self.image_jpeg, self.image_seq

    def get_position(self) -> tuple[float, float]:
        with self.lock:
            return self.pos_x_norm, self.pos_y_norm

    def set_position(self, x_norm: float, y_norm: float) -> None:
        with self.lock:
            self.pos_x_norm = x_norm
            self.pos_y_norm = y_norm


STATE = State()


async def handle_sender(ws) -> None:
    print("sender connected", flush=True)
    try:
        async for msg in ws:
            if isinstance(msg, str):
                try:
                    x, y, _, default_cursor, rmb = decode_position(msg)
                except Exception:
                    continue
                STATE.update(x, y, default_cursor, rmb)
            else:
                try:
                    jpeg = decode_image(msg)
                except Exception:
                    continue
                STATE.update_image(jpeg)
    finally:
        print("sender disconnected", flush=True)


def run_server(port: int) -> None:
    async def serve() -> None:
        async with websockets.serve(handle_sender, "0.0.0.0", port, max_size=None):
            await asyncio.Future()
    asyncio.run(serve())


def set_clickthrough(hwnd: int, enable: bool) -> None:
    style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    style |= WS_EX_LAYERED | WS_EX_TOOLWINDOW
    if enable:
        style |= WS_EX_TRANSPARENT
    else:
        style &= ~WS_EX_TRANSPARENT
    user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)


def _decode_cur(data: bytes, target_size: int) -> QtGui.QImage:
    # PIL's CUR plugin drops the 32bpp alpha channel and returns RGB, which
    # gives a black box around the cursor. Parse the file directly so we keep
    # both the embedded BGRA pixels (32bpp) and the AND mask (24bpp fallback).
    _, type_, count = struct.unpack_from("<HHH", data, 0)
    if type_ != 2:
        raise ValueError("not a CUR file")

    # Pick the entry whose width is closest to target_size, preferring entries
    # that are >= target_size so we only ever downscale.
    best = None
    best_score = None
    for i in range(count):
        eoff = 6 + i * 16
        w_b = data[eoff]
        ioff = struct.unpack_from("<I", data, eoff + 12)[0]
        w = 256 if w_b == 0 else w_b
        score = (0, w - target_size) if w >= target_size else (1, target_size - w)
        if best_score is None or score < best_score:
            best_score = score
            best = (w, ioff)

    _, ioff = best
    bi_size, bi_w, bi_h, _, bi_bpp, _ = struct.unpack_from("<IiiHHI", data, ioff)
    img_h = bi_h // 2  # BMPs in CUR store XOR pixels + AND mask stacked
    pixel_off = ioff + bi_size

    if bi_bpp == 32:
        row_bytes = bi_w * 4
        # BMP is bottom-up; flip rows. Bytes are already BGRA so Format_BGRA8888
        # avoids an explicit byte swap.
        rows = [
            data[pixel_off + (img_h - 1 - y) * row_bytes : pixel_off + (img_h - y) * row_bytes]
            for y in range(img_h)
        ]
        pixels = b"".join(rows)
        return QtGui.QImage(
            pixels, bi_w, img_h, row_bytes, QtGui.QImage.Format.Format_ARGB32,
        ).copy()

    if bi_bpp == 24:
        xor_row_bytes = ((bi_w * 24 + 31) // 32) * 4
        and_row_bytes = ((bi_w + 31) // 32) * 4
        and_off = pixel_off + xor_row_bytes * img_h
        out = bytearray(bi_w * img_h * 4)
        for y in range(img_h):
            src_y = img_h - 1 - y
            xor_row = pixel_off + src_y * xor_row_bytes
            and_row = and_off + src_y * and_row_bytes
            for x in range(bi_w):
                b = data[xor_row + x * 3]
                g = data[xor_row + x * 3 + 1]
                r = data[xor_row + x * 3 + 2]
                bit = (data[and_row + x // 8] >> (7 - (x % 8))) & 1
                o = (y * bi_w + x) * 4
                out[o] = b
                out[o + 1] = g
                out[o + 2] = r
                out[o + 3] = 0 if bit else 255
        return QtGui.QImage(
            bytes(out), bi_w, img_h, bi_w * 4, QtGui.QImage.Format.Format_ARGB32,
        ).copy()

    raise NotImplementedError(f"CUR bpp={bi_bpp} not supported")


def load_cursor_pixmap(filename: str, size_px: int) -> QtGui.QPixmap:
    path = os.path.join(CURSORS_DIR, filename)
    with open(path, "rb") as f:
        data = f.read()
    qimg = _decode_cur(data, size_px)
    if qimg.width() != size_px or qimg.height() != size_px:
        qimg = qimg.scaled(
            size_px, size_px,
            QtCore.Qt.KeepAspectRatio,
            QtCore.Qt.SmoothTransformation,
        )
    return QtGui.QPixmap.fromImage(qimg)


class Overlay(QtWidgets.QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowFlags(
            QtCore.Qt.FramelessWindowHint
            | QtCore.Qt.WindowStaysOnTopHint
            | QtCore.Qt.Tool
        )
        self.setAttribute(QtCore.Qt.WA_TranslucentBackground)
        # Don't steal focus from the game when the overlay shows up.
        self.setAttribute(QtCore.Qt.WA_ShowWithoutActivating)

        screen = QtWidgets.QApplication.primaryScreen().geometry()
        self.screen_w = screen.width()
        self.screen_h = screen.height()
        self.setGeometry(0, 0, self.screen_w, self.screen_h)

        self.cursor_pixmaps = {
            "default": load_cursor_pixmap("Blue.cur", CURSOR_BASE_PX),
            "hover": load_cursor_pixmap("Red.cur", int(CURSOR_BASE_PX * CURSOR_HOVER_SCALE)),
            "click": load_cursor_pixmap("Orange.cur", int(CURSOR_BASE_PX * CURSOR_CLICK_SCALE)),
        }
        self.cursor_key: str | None = None
        self.cursor_x = 0
        self.cursor_y = 0

        self.image_pixmap: QtGui.QPixmap | None = None
        self.image_seq = -1

        self.repos_active = False
        self.drag_offset: tuple[int, int] | None = None

        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(TICK_MS)

    def showEvent(self, event: QtGui.QShowEvent) -> None:
        super().showEvent(event)
        set_clickthrough(int(self.winId()), enable=True)

    def _tick(self) -> None:
        x_norm, y_norm, last, default_cursor, rmb = STATE.snapshot()
        now = int(time.time() * 1000)
        if last == 0 or now - last > STALE_MS:
            self.cursor_key = None
        else:
            if rmb:
                self.cursor_key = "click"
            elif not default_cursor:
                self.cursor_key = "hover"
            else:
                self.cursor_key = "default"
            self.cursor_x = int(x_norm * self.screen_w)
            self.cursor_y = int(y_norm * self.screen_h)

        jpeg, seq = STATE.snapshot_image()
        if jpeg is not None and seq != self.image_seq:
            self.image_seq = seq
            pix = QtGui.QPixmap()
            if pix.loadFromData(jpeg, "JPEG"):
                self.image_pixmap = pix

        self.update()

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.SmoothPixmapTransform, True)

        if self.image_pixmap is not None:
            ix_norm, iy_norm = STATE.get_position()
            ix = int(ix_norm * self.screen_w)
            iy = int(iy_norm * self.screen_h)
            p.drawPixmap(ix, iy, self.image_pixmap)
            if self.repos_active:
                pen = QtGui.QPen(QtGui.QColor("#FF1493"), 2, QtCore.Qt.DashLine)
                p.setPen(pen)
                p.setBrush(QtCore.Qt.NoBrush)
                p.drawRect(ix, iy, self.image_pixmap.width(), self.image_pixmap.height())

        if self.cursor_key is not None:
            pix = self.cursor_pixmaps[self.cursor_key]
            # Center the pixmap on the reported cursor position so scaling
            # (hover/click states) doesn't shift the visual anchor.
            p.drawPixmap(
                self.cursor_x - pix.width() // 2,
                self.cursor_y - pix.height() // 2,
                pix,
            )

    def enter_reposition(self) -> None:
        self.repos_active = True
        set_clickthrough(int(self.winId()), enable=False)

    def exit_reposition(self) -> None:
        self.repos_active = False
        self.drag_offset = None
        set_clickthrough(int(self.winId()), enable=True)

    def _image_rect(self) -> QtCore.QRect | None:
        if self.image_pixmap is None:
            return None
        ix_norm, iy_norm = STATE.get_position()
        ix = int(ix_norm * self.screen_w)
        iy = int(iy_norm * self.screen_h)
        return QtCore.QRect(ix, iy, self.image_pixmap.width(), self.image_pixmap.height())

    def mousePressEvent(self, event: QtGui.QMouseEvent) -> None:
        if not self.repos_active or event.button() != QtCore.Qt.LeftButton:
            return
        rect = self._image_rect()
        if rect is None or not rect.contains(event.pos()):
            return
        self.drag_offset = (event.pos().x() - rect.x(), event.pos().y() - rect.y())

    def mouseMoveEvent(self, event: QtGui.QMouseEvent) -> None:
        if not self.repos_active or self.drag_offset is None:
            return
        ox, oy = self.drag_offset
        new_x = max(0, event.pos().x() - ox)
        new_y = max(0, event.pos().y() - oy)
        STATE.set_position(new_x / self.screen_w, new_y / self.screen_h)

    def mouseReleaseEvent(self, event: QtGui.QMouseEvent) -> None:
        self.drag_offset = None


class ControlWindow(QtWidgets.QWidget):
    def __init__(self, overlay: Overlay) -> None:
        super().__init__()
        self.overlay = overlay
        self.setWindowTitle("Akimbo host")
        self.setWindowFlag(QtCore.Qt.WindowStaysOnTopHint, True)
        self.setFixedSize(240, 90)
        self.active = False

        self.btn = QtWidgets.QPushButton("Reposition stream")
        self.btn.clicked.connect(self._toggle)
        self.status = QtWidgets.QLabel("ready")
        self.status.setStyleSheet("color: #666;")

        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(self.btn)
        layout.addWidget(self.status)

    def _toggle(self) -> None:
        self.active = not self.active
        if self.active:
            self.overlay.enter_reposition()
            self.btn.setText("Done")
            self.status.setText("drag the stream to a new spot")
        else:
            self.overlay.exit_reposition()
            self.btn.setText("Reposition stream")
            self.status.setText("locked")

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        QtWidgets.QApplication.instance().quit()
        super().closeEvent(event)


def main() -> None:
    parser = argparse.ArgumentParser(description="Akimbo host")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    threading.Thread(target=run_server, args=(args.port,), daemon=True).start()
    print(f"listening on ws://0.0.0.0:{args.port}", flush=True)

    app = QtWidgets.QApplication(sys.argv)
    overlay = Overlay()
    overlay.show()
    control = ControlWindow(overlay)
    control.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
