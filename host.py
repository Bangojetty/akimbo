import argparse
import asyncio
import ctypes
import io
import threading
import time
import tkinter as tk

import websockets
from PIL import Image, ImageTk

from protocol import decode_image, decode_position

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
        self.image_jpeg: bytes | None = None
        self.image_dest: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
        self.image_seq = 0

    def update(self, x: float, y: float) -> None:
        with self.lock:
            self.x = x
            self.y = y
            self.last_update_ms = int(time.time() * 1000)

    def snapshot(self) -> tuple[float, float, int]:
        with self.lock:
            return self.x, self.y, self.last_update_ms

    def update_image(self, jpeg: bytes, dest: tuple[float, float, float, float]) -> None:
        with self.lock:
            self.image_jpeg = jpeg
            self.image_dest = dest
            self.image_seq += 1

    def snapshot_image(self) -> tuple[bytes | None, tuple[float, float, float, float], int]:
        with self.lock:
            return self.image_jpeg, self.image_dest, self.image_seq


STATE = State()


async def handle_sender(ws) -> None:
    print("sender connected", flush=True)
    try:
        async for msg in ws:
            if isinstance(msg, str):
                try:
                    x, y, _ = decode_position(msg)
                except Exception:
                    continue
                STATE.update(x, y)
            else:
                try:
                    jpeg, dest = decode_image(msg)
                except Exception:
                    continue
                STATE.update_image(jpeg, dest)
    finally:
        print("sender disconnected", flush=True)


def run_server(port: int) -> None:
    async def serve() -> None:
        async with websockets.serve(handle_sender, "0.0.0.0", port, max_size=None):
            await asyncio.Future()
    asyncio.run(serve())


def make_clickthrough(hwnd: int) -> None:
    style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    user32.SetWindowLongW(
        hwnd,
        GWL_EXSTYLE,
        style | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW,
    )


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

    # Image item below the crosshair so the cursor stays visible over the stream.
    image_item = canvas.create_image(0, 0, anchor="nw", state="hidden")

    # Hot pink crosshair with a center gap so the click point stays visible.
    crosshair_color = "#FF1493"
    arm = 14
    gap = 4
    line_w = 2
    h_left = canvas.create_line(0, 0, 0, 0, fill=crosshair_color, width=line_w)
    h_right = canvas.create_line(0, 0, 0, 0, fill=crosshair_color, width=line_w)
    v_top = canvas.create_line(0, 0, 0, 0, fill=crosshair_color, width=line_w)
    v_bottom = canvas.create_line(0, 0, 0, 0, fill=crosshair_color, width=line_w)
    crosshair_parts = (h_left, h_right, v_top, v_bottom)

    # Click-through must be applied after the HWND exists. Tk wraps its toplevel
    # in an internal parent on Windows, so the actual styled window is GetParent.
    root.update_idletasks()
    hwnd = user32.GetParent(root.winfo_id()) or root.winfo_id()
    make_clickthrough(hwnd)

    image_state = {"seq": -1, "photo": None}

    def render_image() -> None:
        jpeg, dest, seq = STATE.snapshot_image()
        if jpeg is None or seq == image_state["seq"]:
            return
        image_state["seq"] = seq
        try:
            pil = Image.open(io.BytesIO(jpeg))
            dx = int(dest[0] * width)
            dy = int(dest[1] * height)
            dw = max(1, int(dest[2] * width))
            dh = max(1, int(dest[3] * height))
            pil = pil.resize((dw, dh))
            photo = ImageTk.PhotoImage(pil)
            image_state["photo"] = photo  # keep reference; Tk does not own it
            canvas.itemconfigure(image_item, image=photo, state="normal")
            canvas.coords(image_item, dx, dy)
            canvas.tag_lower(image_item)  # keep crosshair on top
        except Exception as e:
            print(f"image render error: {e}", flush=True)

    def tick() -> None:
        x_norm, y_norm, last = STATE.snapshot()
        now = int(time.time() * 1000)
        if last == 0 or now - last > STALE_MS:
            for part in crosshair_parts:
                canvas.itemconfigure(part, state="hidden")
        else:
            cx = int(x_norm * width)
            cy = int(y_norm * height)
            canvas.coords(h_left, cx - arm, cy, cx - gap, cy)
            canvas.coords(h_right, cx + gap, cy, cx + arm, cy)
            canvas.coords(v_top, cx, cy - arm, cx, cy - gap)
            canvas.coords(v_bottom, cx, cy + gap, cx, cy + arm)
            for part in crosshair_parts:
                canvas.itemconfigure(part, state="normal")
        render_image()
        root.after(TICK_MS, tick)

    tick()
    root.mainloop()


def main() -> None:
    parser = argparse.ArgumentParser(description="Akimbo host")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    threading.Thread(target=run_server, args=(args.port,), daemon=True).start()
    print(f"listening on ws://0.0.0.0:{args.port}", flush=True)

    run_overlay()


if __name__ == "__main__":
    main()
