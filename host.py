import argparse
import asyncio
import ctypes
import threading
import time
import tkinter as tk

import websockets

from protocol import decode_position

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

    def update(self, x: float, y: float) -> None:
        with self.lock:
            self.x = x
            self.y = y
            self.last_update_ms = int(time.time() * 1000)

    def snapshot(self) -> tuple[float, float, int]:
        with self.lock:
            return self.x, self.y, self.last_update_ms


STATE = State()


async def handle_sender(ws) -> None:
    print("sender connected", flush=True)
    try:
        async for msg in ws:
            try:
                x, y, _ = decode_position(msg)
            except Exception:
                continue
            STATE.update(x, y)
    finally:
        print("sender disconnected", flush=True)


def run_server(port: int) -> None:
    async def serve() -> None:
        async with websockets.serve(handle_sender, "0.0.0.0", port):
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

    radius = 12
    cursor = canvas.create_oval(
        -radius, -radius, radius, radius,
        outline="cyan", width=3, fill="",
    )

    # Click-through must be applied after the HWND exists. Tk wraps its toplevel
    # in an internal parent on Windows, so the actual styled window is GetParent.
    root.update_idletasks()
    hwnd = user32.GetParent(root.winfo_id()) or root.winfo_id()
    make_clickthrough(hwnd)

    def tick() -> None:
        x_norm, y_norm, last = STATE.snapshot()
        now = int(time.time() * 1000)
        if last == 0 or now - last > STALE_MS:
            canvas.itemconfigure(cursor, state="hidden")
        else:
            px = int(x_norm * width)
            py = int(y_norm * height)
            canvas.coords(cursor, px - radius, py - radius, px + radius, py + radius)
            canvas.itemconfigure(cursor, state="normal")
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
