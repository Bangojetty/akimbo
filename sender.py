import argparse
import asyncio
import atexit
import ctypes
import io
import json
import os
import os.path
import sys
import threading
import tkinter as tk
from ctypes import wintypes
from tkinter import ttk

import mss
import websockets
from PIL import Image

from protocol import encode_image, encode_position

CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "akimbo_config.json"
)


def load_config() -> dict:
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def save_config(cfg: dict) -> None:
    try:
        with open(CONFIG_PATH, "w") as f:
            json.dump(cfg, f, indent=2)
    except OSError as e:
        print(f"failed to save config: {e}", file=sys.stderr, flush=True)

POLL_HZ = 120

user32 = ctypes.windll.user32
# Match physical pixels so coordinate normalization lines up across DPI-scaled displays.
user32.SetProcessDPIAware()

VK_OEM_3 = 0xC0  # backtick / tilde key on US layouts
VK_RBUTTON = 0x02
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_SCANCODE = 0x0008
INPUT_KEYBOARD = 1
IDC_ARROW = 32512
DEFAULT_CURSOR_HANDLE = ctypes.windll.user32.LoadCursorW(0, IDC_ARROW)
CURSOR_SHOWING = 0x00000001
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

# Apps whose custom cursors should still be reported as "default" (no hover-state
# swap on the host) — typically games we know use their own cursor system-wide.
ALWAYS_DEFAULT_EXES = {"league of legends.exe", "leagueclient.exe"}

kernel32 = ctypes.windll.kernel32
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
]
kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL

# Function-key scancodes (set 1). F11/F12 are non-contiguous with the F1-F10 block.
FUNCTION_KEY_SCANCODES = {
    "F1": 0x3B, "F2": 0x3C, "F3": 0x3D, "F4": 0x3E,
    "F5": 0x3F, "F6": 0x40, "F7": 0x41, "F8": 0x42,
    "F9": 0x43, "F10": 0x44, "F11": 0x57, "F12": 0x58,
}

_held_key_name = "F2"
_held_scancode = FUNCTION_KEY_SCANCODES["F2"]
_key_held = False


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


def hold_key() -> None:
    global _key_held
    if _key_held:
        return
    _send_scancode(_held_scancode, key_up=False)
    _key_held = True
    print(f"{_held_key_name} held", file=sys.stderr, flush=True)


def release_key() -> None:
    global _key_held
    if not _key_held:
        return
    _send_scancode(_held_scancode, key_up=True)
    _key_held = False
    print(f"{_held_key_name} released", file=sys.stderr, flush=True)


def toggle_key() -> None:
    if _key_held:
        release_key()
    else:
        hold_key()


def set_held_key(name: str) -> None:
    """Switch which function key is being held. Re-holds the new key if the old one was held."""
    global _held_key_name, _held_scancode
    if name not in FUNCTION_KEY_SCANCODES or name == _held_key_name:
        return
    was_held = _key_held
    if was_held:
        release_key()
    _held_key_name = name
    _held_scancode = FUNCTION_KEY_SCANCODES[name]
    if was_held:
        hold_key()


def is_key_down(vk: int) -> bool:
    return bool(user32.GetAsyncKeyState(vk) & 0x8000)


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class CURSORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("hCursor", wintypes.HANDLE),
        ("ptScreenPos", POINT),
    ]


def get_cursor_position() -> tuple[int, int]:
    pt = POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


_foreground_cache = {"hwnd": 0, "always_default": False}


def _foreground_exe_name() -> str:
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return ""
    pid = wintypes.DWORD(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return ""
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(260)
        size = wintypes.DWORD(len(buf))
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return ""
        return os.path.basename(buf.value)
    finally:
        kernel32.CloseHandle(handle)


def foreground_is_always_default() -> bool:
    """True if the foreground process is in the always-default whitelist (e.g. League)."""
    hwnd = user32.GetForegroundWindow()
    if hwnd != _foreground_cache["hwnd"]:
        _foreground_cache["hwnd"] = hwnd
        _foreground_cache["always_default"] = (
            _foreground_exe_name().lower() in ALWAYS_DEFAULT_EXES
        )
    return _foreground_cache["always_default"]


def is_default_cursor() -> bool:
    # Games like League use their own cursor system-wide, which would otherwise
    # constantly trigger the hover state. Force default while focused.
    if foreground_is_always_default():
        return True
    info = CURSORINFO()
    info.cbSize = ctypes.sizeof(info)
    if not user32.GetCursorInfo(ctypes.byref(info)):
        return True
    # Windows suppresses the cursor during idle / touch interaction. The hCursor
    # reported in that state is not the arrow handle, which would falsely trigger
    # the hover state. Treat hidden/suppressed as default.
    if not (info.flags & CURSOR_SHOWING):
        return True
    return info.hCursor == DEFAULT_CURSOR_HANDLE


def get_screen_size() -> tuple[int, int]:
    return user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)


def poll_toggle(prev_down: bool) -> bool:
    now_down = is_key_down(VK_OEM_3)
    if now_down and not prev_down:
        toggle_key()
    return now_down


# Refresh the held key every Nth tick. At 120 Hz polling, every 4 ticks ≈ 30 Hz,
# which matches typical OS auto-repeat for a physically held key. Required because
# games like League can lose the synthetic held state when other keypresses arrive.
KEY_REFRESH_EVERY = 4


async def cursor_loop(ws) -> None:
    width, height = get_screen_size()
    interval = 1.0 / POLL_HZ
    toggle_prev = is_key_down(VK_OEM_3)
    tick = 0
    while True:
        toggle_prev = poll_toggle(toggle_prev)
        if _key_held and tick % KEY_REFRESH_EVERY == 0:
            _send_scancode(_held_scancode, key_up=False)
        x, y = get_cursor_position()
        await ws.send(encode_position(
            x / width, y / height,
            is_default_cursor(),
            is_key_down(VK_RBUTTON),
        ))
        await asyncio.sleep(interval)
        tick += 1


def _capture_jpeg(monitor: dict, quality: int) -> bytes:
    with mss.mss() as sct:
        shot = sct.grab(monitor)
        pil = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
        buf = io.BytesIO()
        pil.save(buf, format="JPEG", quality=quality)
        return buf.getvalue()


async def image_loop(ws, get_region, fps: int, quality: int) -> None:
    interval = 1.0 / fps
    loop = asyncio.get_event_loop()
    while True:
        region = get_region()
        if region is not None:
            monitor = {
                "left": region[0], "top": region[1],
                "width": region[2], "height": region[3],
            }
            jpeg = await loop.run_in_executor(None, _capture_jpeg, monitor, quality)
            await ws.send(encode_image(jpeg))
        await asyncio.sleep(interval)


async def run_session(uri: str, get_region, fps: int, quality: int, on_status) -> None:
    while True:
        try:
            on_status(f"connecting to {uri}…")
            async with websockets.connect(uri, max_size=None) as ws:
                on_status(f"connected to {uri}")
                tasks = [
                    asyncio.create_task(cursor_loop(ws)),
                    asyncio.create_task(image_loop(ws, get_region, fps, quality)),
                ]
                done, pending = await asyncio.wait(
                    tasks, return_when=asyncio.FIRST_EXCEPTION
                )
                for t in pending:
                    t.cancel()
                for t in done:
                    t.result()
        except asyncio.CancelledError:
            on_status("stopped")
            raise
        except (OSError, websockets.exceptions.WebSocketException) as e:
            on_status(f"connection lost: {e}; retrying in 1s")
            await asyncio.sleep(1.0)


class StreamController:
    """Owns the asyncio loop+thread for the streaming session."""

    def __init__(self) -> None:
        self.thread: threading.Thread | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.task: asyncio.Task | None = None

    def is_running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def start(self, uri: str, get_region, fps: int, quality: int, on_status) -> None:
        if self.is_running():
            return
        ready = threading.Event()

        def thread_main() -> None:
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            self.task = self.loop.create_task(
                run_session(uri, get_region, fps, quality, on_status)
            )
            ready.set()
            try:
                self.loop.run_until_complete(self.task)
            except asyncio.CancelledError:
                pass
            finally:
                self.loop.close()

        self.thread = threading.Thread(target=thread_main, daemon=True)
        self.thread.start()
        ready.wait(timeout=2.0)

    def stop(self) -> None:
        if self.loop and self.task and not self.task.done():
            self.loop.call_soon_threadsafe(self.task.cancel)
        if self.thread:
            self.thread.join(timeout=3.0)
        self.thread = None
        self.loop = None
        self.task = None


class RegionPicker:
    """Fullscreen drag-to-select rectangle picker. Returns (x, y, w, h) screen pixels."""

    def __init__(self, parent: tk.Misc, on_done) -> None:
        self.on_done = on_done
        self.win = tk.Toplevel(parent)
        self.win.attributes("-fullscreen", True)
        self.win.attributes("-alpha", 0.3)
        self.win.attributes("-topmost", True)
        self.win.configure(bg="black", cursor="cross")
        self.canvas = tk.Canvas(self.win, bg="black", highlightthickness=0, cursor="cross")
        self.canvas.pack(fill="both", expand=True)
        self.rect_id: int | None = None
        self.start_screen: tuple[int, int] = (0, 0)
        self.start_canvas: tuple[int, int] = (0, 0)
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.win.bind("<Escape>", lambda e: self._cancel())

    def _on_press(self, e: tk.Event) -> None:
        self.start_screen = (e.x_root, e.y_root)
        self.start_canvas = (e.x, e.y)
        if self.rect_id is not None:
            self.canvas.delete(self.rect_id)
        self.rect_id = self.canvas.create_rectangle(
            e.x, e.y, e.x, e.y, outline="#FF1493", width=2,
        )

    def _on_drag(self, e: tk.Event) -> None:
        if self.rect_id is None:
            return
        sx, sy = self.start_canvas
        self.canvas.coords(self.rect_id, sx, sy, e.x, e.y)

    def _on_release(self, e: tk.Event) -> None:
        sx, sy = self.start_screen
        x1, y1 = min(sx, e.x_root), min(sy, e.y_root)
        x2, y2 = max(sx, e.x_root), max(sy, e.y_root)
        rect = (x1, y1, x2 - x1, y2 - y1)
        self.win.destroy()
        if rect[2] >= 8 and rect[3] >= 8:
            self.on_done(rect)

    def _cancel(self) -> None:
        self.win.destroy()


class SenderUI:
    def __init__(self, root: tk.Tk, host: str, port: int, fps: int, quality: int,
                 config: dict) -> None:
        self.root = root
        self.fps = fps
        self.quality = quality
        self.region: tuple[int, int, int, int] | None = None
        self.region_lock = threading.Lock()
        self.controller = StreamController()

        # Config (last saved settings) wins over CLI defaults so re-opening
        # the app reproduces the previous session.
        cfg_host = config.get("host", host)
        cfg_port = int(config.get("port", port))
        cfg_held_key = config.get("held_key", _held_key_name)
        cfg_region = config.get("region")

        self.host_var = tk.StringVar(value=cfg_host)
        self.port_var = tk.StringVar(value=str(cfg_port))
        self.region_var = tk.StringVar(value="(none — streams cursor only)")
        self.status_var = tk.StringVar(value="idle")
        self.start_button_text = tk.StringVar(value="Start")
        self.held_key_var = tk.StringVar(value=cfg_held_key)

        if cfg_held_key in FUNCTION_KEY_SCANCODES and cfg_held_key != _held_key_name:
            set_held_key(cfg_held_key)
        if cfg_region and len(cfg_region) == 4:
            self._apply_region(tuple(cfg_region))

        root.title("Akimbo sender")
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._build()
        # Auto-start streaming after the UI has settled.
        self.root.after(100, self._toggle_stream)

    def _build(self) -> None:
        frm = ttk.Frame(self.root, padding=10)
        frm.grid(sticky="nsew")
        ttk.Label(frm, text="Host IP:").grid(row=0, column=0, sticky="e", padx=4, pady=2)
        ttk.Entry(frm, textvariable=self.host_var, width=22).grid(row=0, column=1, sticky="w")
        ttk.Label(frm, text="Port:").grid(row=1, column=0, sticky="e", padx=4, pady=2)
        ttk.Entry(frm, textvariable=self.port_var, width=8).grid(row=1, column=1, sticky="w")
        ttk.Label(frm, text="Region:").grid(row=2, column=0, sticky="e", padx=4, pady=2)
        ttk.Label(frm, textvariable=self.region_var).grid(row=2, column=1, sticky="w")
        ttk.Label(frm, text="Held key:").grid(row=3, column=0, sticky="e", padx=4, pady=2)
        held_key_combo = ttk.Combobox(
            frm,
            textvariable=self.held_key_var,
            values=list(FUNCTION_KEY_SCANCODES.keys()),
            state="readonly",
            width=6,
        )
        held_key_combo.grid(row=3, column=1, sticky="w")
        held_key_combo.bind("<<ComboboxSelected>>", self._on_held_key_change)
        ttk.Button(frm, text="Select region…", command=self._select_region).grid(
            row=4, column=0, columnspan=2, pady=(6, 2), sticky="ew"
        )
        ttk.Button(frm, textvariable=self.start_button_text, command=self._toggle_stream).grid(
            row=5, column=0, columnspan=2, pady=(2, 6), sticky="ew"
        )
        ttk.Label(frm, textvariable=self.status_var, foreground="#555").grid(
            row=6, column=0, columnspan=2, sticky="w"
        )
        ttk.Label(
            frm,
            text="held by default · backtick (`) toggles",
            foreground="#888",
        ).grid(row=7, column=0, columnspan=2, sticky="w", pady=(8, 0))

    def _on_held_key_change(self, _event=None) -> None:
        set_held_key(self.held_key_var.get())
        self._save()

    def _get_region(self) -> tuple[int, int, int, int] | None:
        with self.region_lock:
            return self.region

    def _apply_region(self, rect: tuple[int, int, int, int]) -> None:
        with self.region_lock:
            self.region = rect
        self.region_var.set(f"{rect[2]}×{rect[3]} at ({rect[0]}, {rect[1]})")

    def _set_region(self, rect: tuple[int, int, int, int]) -> None:
        self._apply_region(rect)
        self._save()

    def _save(self) -> None:
        try:
            port = int(self.port_var.get())
        except ValueError:
            port = 8765
        save_config({
            "host": self.host_var.get(),
            "port": port,
            "region": list(self.region) if self.region else None,
            "held_key": self.held_key_var.get(),
        })

    def _select_region(self) -> None:
        self.root.withdraw()

        def done(rect: tuple[int, int, int, int]) -> None:
            self._set_region(rect)
            self.root.deiconify()

        # Restore the window even if the user hits Escape.
        picker = RegionPicker(self.root, done)
        picker.win.bind("<Destroy>", lambda e: self.root.deiconify(), add="+")

    def _set_status(self, text: str) -> None:
        # Called from background thread; route to Tk thread.
        self.root.after(0, self.status_var.set, text)

    def _toggle_stream(self) -> None:
        if self.controller.is_running():
            self.controller.stop()
            self.start_button_text.set("Start")
            self.status_var.set("stopped")
            return
        try:
            port = int(self.port_var.get())
        except ValueError:
            self.status_var.set("invalid port")
            return
        uri = f"ws://{self.host_var.get()}:{port}"
        self.controller.start(uri, self._get_region, self.fps, self.quality, self._set_status)
        self.start_button_text.set("Stop")
        self._save()

    def _on_close(self) -> None:
        self.controller.stop()
        self.root.destroy()


def main() -> None:
    parser = argparse.ArgumentParser(description="Akimbo sender")
    parser.add_argument("--host", default="127.0.0.1", help="initial host PC address")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--capture-fps", type=int, default=20)
    parser.add_argument("--capture-quality", type=int, default=70)
    args = parser.parse_args()

    atexit.register(release_key)
    hold_key()

    config = load_config()
    root = tk.Tk()
    SenderUI(root, args.host, args.port, args.capture_fps, args.capture_quality, config)
    try:
        root.mainloop()
    except KeyboardInterrupt:
        pass
    finally:
        release_key()


if __name__ == "__main__":
    main()
