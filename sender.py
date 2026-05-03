import argparse
import asyncio
import ctypes
import sys
from ctypes import wintypes

import websockets

from protocol import encode_position

POLL_HZ = 120

user32 = ctypes.windll.user32
# Match physical pixels so coordinate normalization lines up across DPI-scaled displays.
user32.SetProcessDPIAware()


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


def get_cursor_position() -> tuple[int, int]:
    pt = POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def get_screen_size() -> tuple[int, int]:
    return user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)


async def stream_cursor(uri: str) -> None:
    width, height = get_screen_size()
    interval = 1.0 / POLL_HZ
    while True:
        try:
            async with websockets.connect(uri) as ws:
                print(f"connected to {uri}", file=sys.stderr, flush=True)
                while True:
                    x, y = get_cursor_position()
                    await ws.send(encode_position(x / width, y / height))
                    await asyncio.sleep(interval)
        except (OSError, websockets.exceptions.WebSocketException) as e:
            print(f"connection lost: {e}; retrying in 1s", file=sys.stderr, flush=True)
            await asyncio.sleep(1.0)


def main() -> None:
    parser = argparse.ArgumentParser(description="Akimbo sender")
    parser.add_argument("--host", default="127.0.0.1", help="host PC address")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    uri = f"ws://{args.host}:{args.port}"
    try:
        asyncio.run(stream_cursor(uri))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
