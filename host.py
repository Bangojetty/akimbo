import argparse
import asyncio
import ctypes
import io
import os
import threading
import time
import tkinter as tk
from tkinter import ttk

import websockets
from PIL import Image, ImageTk

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

# Tk's transparentcolor on Windows treats this exact color as fully see-through.
TRANSPARENT_KEY = "magenta"


class State:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.x = 0.0
        self.y = 0.0
        self.last_update_ms = 0
        self.default_cursor = True
        self.lmb = False
        self.image_jpeg: bytes | None = None
        self.image_seq = 0
        # Host owns the position. Defaults to top-right corner area.
        self.pos_x_norm = 0.7
        self.pos_y_norm = 0.0

    def update(self, x: float, y: float, default_cursor: bool, lmb: bool) -> None:
        with self.lock:
            self.x = x
            self.y = y
            self.default_cursor = default_cursor
            self.lmb = lmb
            self.last_update_ms = int(time.time() * 1000)

    def snapshot(self) -> tuple[float, float, int, bool, bool]:
        with self.lock:
            return self.x, self.y, self.last_update_ms, self.default_cursor, self.lmb

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
                    x, y, _, default_cursor, lmb = decode_position(msg)
                except Exception:
                    continue
                STATE.update(x, y, default_cursor, lmb)
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


def load_cursor(filename: str, size_px: int) -> ImageTk.PhotoImage:
    path = os.path.join(CURSORS_DIR, filename)
    img = Image.open(path).convert("RGBA")
    img = img.resize((size_px, size_px), Image.LANCZOS)
    # Tk's -transparentcolor only matches the exact magenta key, so anti-aliased
    # edges would blend to a pinkish color and remain opaque (halo). Threshold
    # alpha to binary and composite onto magenta so only fully transparent
    # pixels are keyed out.
    r, g, b, a = img.split()
    mask = a.point(lambda v: 255 if v >= 128 else 0)
    bg = Image.new("RGB", img.size, (255, 0, 255))
    bg.paste(Image.merge("RGB", (r, g, b)), (0, 0), mask)
    return ImageTk.PhotoImage(bg)


def run_overlay() -> None:
    width = user32.GetSystemMetrics(0)
    height = user32.GetSystemMetrics(1)

    root = tk.Tk()
    root.overrideredirect(True)
    root.geometry(f"{width}x{height}+0+0")
    root.attributes("-topmost", True)
    root.configure(bg=TRANSPARENT_KEY)
    root.attributes("-transparentcolor", TRANSPARENT_KEY)

    canvas = tk.Canvas(
        root, width=width, height=height,
        bg=TRANSPARENT_KEY, highlightthickness=0,
    )
    canvas.pack(fill="both", expand=True)

    # Image item below the cursor so the cursor stays visible over the stream.
    image_item = canvas.create_image(0, 0, anchor="nw", state="hidden")

    cursor_images = {
        "default": load_cursor("Blue.cur", CURSOR_BASE_PX),
        "hover": load_cursor("Red.cur", int(CURSOR_BASE_PX * CURSOR_HOVER_SCALE)),
        "click": load_cursor("Orange.cur", int(CURSOR_BASE_PX * CURSOR_CLICK_SCALE)),
    }
    cursor_item = canvas.create_image(
        0, 0, image=cursor_images["default"], state="hidden",
    )

    # Outline shown only during reposition mode.
    reposition_outline = canvas.create_rectangle(
        0, 0, 0, 0, outline="#FF1493", width=2, dash=(6, 4), state="hidden",
    )

    # Click-through must be applied after the HWND exists. Tk wraps its toplevel
    # in an internal parent on Windows, so the actual styled window is GetParent.
    root.update_idletasks()
    hwnd = user32.GetParent(root.winfo_id()) or root.winfo_id()
    set_clickthrough(hwnd, enable=True)

    image_state = {
        "seq": -1,
        "photo": None,
        "size": (0, 0),  # native size of last received frame
    }
    repos = {"active": False, "drag_offset": (0, 0)}

    def update_image_position() -> None:
        x_norm, y_norm = STATE.get_position()
        dx = int(x_norm * width)
        dy = int(y_norm * height)
        canvas.coords(image_item, dx, dy)
        w, h = image_state["size"]
        if w > 0 and h > 0:
            canvas.coords(reposition_outline, dx, dy, dx + w, dy + h)

    def render_image() -> None:
        jpeg, seq = STATE.snapshot_image()
        if jpeg is None or seq == image_state["seq"]:
            return
        image_state["seq"] = seq
        try:
            pil = Image.open(io.BytesIO(jpeg))
            photo = ImageTk.PhotoImage(pil)
            image_state["photo"] = photo  # Tk does not retain the reference
            image_state["size"] = pil.size
            canvas.itemconfigure(image_item, image=photo, state="normal")
            canvas.tag_lower(image_item)  # keep cursor on top
            update_image_position()
        except Exception as e:
            print(f"image render error: {e}", flush=True)

    def on_drag_press(e: tk.Event) -> None:
        if not repos["active"]:
            return
        cx, cy = canvas.coords(image_item)
        repos["drag_offset"] = (e.x - cx, e.y - cy)

    def on_drag_motion(e: tk.Event) -> None:
        if not repos["active"]:
            return
        ox, oy = repos["drag_offset"]
        new_x = max(0, e.x - ox)
        new_y = max(0, e.y - oy)
        STATE.set_position(new_x / width, new_y / height)
        update_image_position()

    canvas.tag_bind(image_item, "<ButtonPress-1>", on_drag_press)
    canvas.tag_bind(image_item, "<B1-Motion>", on_drag_motion)

    def enter_reposition() -> None:
        repos["active"] = True
        set_clickthrough(hwnd, enable=False)
        canvas.itemconfigure(reposition_outline, state="normal")
        update_image_position()

    def exit_reposition() -> None:
        repos["active"] = False
        set_clickthrough(hwnd, enable=True)
        canvas.itemconfigure(reposition_outline, state="hidden")

    def tick() -> None:
        x_norm, y_norm, last, default_cursor, lmb = STATE.snapshot()
        now = int(time.time() * 1000)
        if last == 0 or now - last > STALE_MS:
            canvas.itemconfigure(cursor_item, state="hidden")
        else:
            # Click takes precedence over hover state.
            if lmb:
                key = "click"
            elif not default_cursor:
                key = "hover"
            else:
                key = "default"
            cx = int(x_norm * width)
            cy = int(y_norm * height)
            canvas.coords(cursor_item, cx, cy)
            canvas.itemconfigure(cursor_item, image=cursor_images[key], state="normal")
            canvas.tag_raise(cursor_item)
        render_image()
        root.after(TICK_MS, tick)

    tick()
    build_control_window(root, enter_reposition, exit_reposition)
    root.mainloop()


def build_control_window(parent: tk.Tk, enter_repos, exit_repos) -> tk.Toplevel:
    win = tk.Toplevel(parent)
    win.title("Akimbo host")
    win.attributes("-topmost", True)
    win.resizable(False, False)
    state = {"active": False}
    btn_text = tk.StringVar(value="Reposition stream")
    status = tk.StringVar(value="ready")

    def toggle() -> None:
        state["active"] = not state["active"]
        if state["active"]:
            enter_repos()
            btn_text.set("Done")
            status.set("drag the stream to a new spot")
        else:
            exit_repos()
            btn_text.set("Reposition stream")
            status.set("locked")

    frm = ttk.Frame(win, padding=10)
    frm.grid()
    ttk.Button(frm, textvariable=btn_text, command=toggle, width=22).grid(row=0, column=0)
    ttk.Label(frm, textvariable=status, foreground="#666").grid(
        row=1, column=0, pady=(6, 0), sticky="w"
    )
    return win


def main() -> None:
    parser = argparse.ArgumentParser(description="Akimbo host")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    threading.Thread(target=run_server, args=(args.port,), daemon=True).start()
    print(f"listening on ws://0.0.0.0:{args.port}", flush=True)

    run_overlay()


if __name__ == "__main__":
    main()
