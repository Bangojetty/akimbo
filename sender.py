import argparse
import asyncio
import atexit
import ctypes
import io
import sys
from ctypes import wintypes

import mss
import websockets
from PIL import Image

from protocol import encode_image, encode_position

POLL_HZ = 120

user32 = ctypes.windll.user32
# Match physical pixels so coordinate normalization lines up across DPI-scaled displays.
user32.SetProcessDPIAware()

VK_OEM_3 = 0xC0  # backtick / tilde key on US layouts
SC_F2 = 0x3C
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_SCANCODE = 0x0008
INPUT_KEYBOARD = 1
_f2_held = False


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUT_UNION)]


def _send_scancode(scan: int, key_up: bool) -> None:
    flags = KEYEVENTF_SCANCODE | (KEYEVENTF_KEYUP if key_up else 0)
    inp = INPUT()
    inp.type = INPUT_KEYBOARD
    inp.u.ki = KEYBDINPUT(0, scan, flags, 0, None)
    user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp))


def hold_f2() -> None:
    global _f2_held
    if _f2_held:
        return
    _send_scancode(SC_F2, key_up=False)
    _f2_held = True
    print("F2 held", file=sys.stderr, flush=True)


def release_f2() -> None:
    global _f2_held
    if not _f2_held:
        return
    _send_scancode(SC_F2, key_up=True)
    _f2_held = False
    print("F2 released", file=sys.stderr, flush=True)


def toggle_f2() -> None:
    if _f2_held:
        release_f2()
    else:
        hold_f2()


def is_key_down(vk: int) -> bool:
    return bool(user32.GetAsyncKeyState(vk) & 0x8000)


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


def get_cursor_position() -> tuple[int, int]:
    pt = POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def get_screen_size() -> tuple[int, int]:
    return user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)


def poll_toggle(prev_down: bool) -> bool:
    now_down = is_key_down(VK_OEM_3)
    if now_down and not prev_down:
        toggle_f2()
    return now_down


# Refresh held F2 every Nth tick. At 120 Hz polling, every 4 ticks ≈ 30 Hz, which
# matches typical OS auto-repeat for a physically held key. Required because games
# like League can lose the synthetic held state when other keypresses arrive.
F2_REFRESH_EVERY = 4


async def cursor_loop(ws) -> None:
    width, height = get_screen_size()
    interval = 1.0 / POLL_HZ
    toggle_prev = is_key_down(VK_OEM_3)
    tick = 0
    while True:
        toggle_prev = poll_toggle(toggle_prev)
        if _f2_held and tick % F2_REFRESH_EVERY == 0:
            _send_scancode(SC_F2, key_up=False)
        x, y = get_cursor_position()
        await ws.send(encode_position(x / width, y / height))
        await asyncio.sleep(interval)
        tick += 1


def _capture_jpeg(monitor: dict, quality: int) -> bytes:
    with mss.mss() as sct:
        shot = sct.grab(monitor)
        pil = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
        buf = io.BytesIO()
        pil.save(buf, format="JPEG", quality=quality)
        return buf.getvalue()


async def image_loop(ws, src_rect, dest_rect, fps: int, quality: int) -> None:
    interval = 1.0 / fps
    monitor = {
        "left": src_rect[0], "top": src_rect[1],
        "width": src_rect[2], "height": src_rect[3],
    }
    loop = asyncio.get_event_loop()
    while True:
        jpeg = await loop.run_in_executor(None, _capture_jpeg, monitor, quality)
        await ws.send(encode_image(jpeg, dest_rect))
        await asyncio.sleep(interval)


async def run(uri: str, src_rect, dest_rect, fps: int, quality: int) -> None:
    while True:
        try:
            async with websockets.connect(uri, max_size=None) as ws:
                print(f"connected to {uri}", file=sys.stderr, flush=True)
                tasks = [asyncio.create_task(cursor_loop(ws))]
                if src_rect is not None and dest_rect is not None:
                    tasks.append(asyncio.create_task(
                        image_loop(ws, src_rect, dest_rect, fps, quality)
                    ))
                done, pending = await asyncio.wait(
                    tasks, return_when=asyncio.FIRST_EXCEPTION
                )
                for t in pending:
                    t.cancel()
                for t in done:
                    t.result()
        except (OSError, websockets.exceptions.WebSocketException) as e:
            print(f"connection lost: {e}; retrying in 1s", file=sys.stderr, flush=True)
            await asyncio.sleep(1.0)


def parse_int_rect(s: str) -> tuple[int, int, int, int]:
    parts = [int(p) for p in s.split(",")]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("expected x,y,w,h")
    return tuple(parts)  # type: ignore[return-value]


def parse_float_rect(s: str) -> tuple[float, float, float, float]:
    parts = [float(p) for p in s.split(",")]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("expected x,y,w,h")
    return tuple(parts)  # type: ignore[return-value]


def main() -> None:
    parser = argparse.ArgumentParser(description="Akimbo sender")
    parser.add_argument("--host", default="127.0.0.1", help="host PC address")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--src-rect", type=parse_int_rect, default=None,
        help="sender pixel rect to capture, x,y,w,h (e.g. 0,0,800,450). Omit to disable streaming.",
    )
    parser.add_argument(
        "--dest-rect", type=parse_float_rect, default=None,
        help="host overlay destination rect, normalized 0..1, x,y,w,h (e.g. 0.7,0,0.3,0.17).",
    )
    parser.add_argument("--capture-fps", type=int, default=20)
    parser.add_argument("--capture-quality", type=int, default=70)
    args = parser.parse_args()

    if (args.src_rect is None) ^ (args.dest_rect is None):
        parser.error("--src-rect and --dest-rect must be provided together")

    uri = f"ws://{args.host}:{args.port}"
    atexit.register(release_f2)
    hold_f2()
    try:
        asyncio.run(run(
            uri, args.src_rect, args.dest_rect,
            args.capture_fps, args.capture_quality,
        ))
    except KeyboardInterrupt:
        pass
    finally:
        release_f2()


if __name__ == "__main__":
    main()
