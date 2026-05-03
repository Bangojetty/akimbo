# Akimbo — Product Requirements Document

## 1. Overview

Akimbo is a cursor-mirroring application that displays a "ghost" second cursor on one computer (the **host**) whose position is streamed over a WebSocket connection from a second computer (the **sender**). The host machine sees both its own native cursor and the remote cursor as a visual overlay.

The driving use case: piloting two League of Legends champions simultaneously by running the game on the host PC and using a second PC to control where a second set of inputs would go, visualized as a mirrored cursor on the host's screen.

## 2. Goals

- Display a second cursor on the host machine that mirrors the position of the cursor on the sender machine in real time.
- Achieve low enough latency (< 30 ms perceptible end-to-end) for the mirrored cursor to feel usable in a fast-paced game like League of Legends.
- Run as an always-on-top, click-through overlay on Windows so it never interferes with the game underneath.
- Be trivial to start: launch sender on PC A, launch host on PC B, the cursors connect.

## 3. Non-Goals (v1)

- Sending actual mouse clicks or keyboard input from the sender to the host (visual mirror only).
- Cross-OS support — Windows only for v1.
- Multi-user / many-to-many cursor mirroring (one sender, one host).
- Authentication, encryption, NAT traversal, or running over the public internet. v1 assumes both PCs are on the same LAN.
- Anti-cheat compatibility guarantees. The overlay is purely cosmetic and reads no game memory, but Riot's Vanguard behavior toward overlays is out of scope to formally certify.

## 4. Users & Use Case

**Primary user:** the project owner, playing 1v1 / custom / bot games of League of Legends and wanting to coordinate two champions at once across two PCs.

**Flow:**
1. League runs on the host PC (PC B), windowed or borderless fullscreen.
2. Sender app runs on PC A. The user moves the mouse on PC A.
3. The mirrored cursor appears as an overlay on PC B at the corresponding screen coordinates.
4. The user looks at PC B's screen to see both cursors and plans actions for both champions accordingly.

## 5. Functional Requirements

### 5.1 Sender (PC A)
- Capture the system cursor's screen position at a high polling rate (target: 120 Hz, minimum 60 Hz).
- Normalize coordinates to a `[0.0, 1.0]` range over the sender's primary display so resolution differences between machines don't break the mapping.
- Open a WebSocket connection to a configurable host address (`ws://<host-ip>:<port>`).
- Send position updates as small JSON or binary frames: `{ "x": float, "y": float, "t": timestamp_ms }`.
- Reconnect automatically if the connection drops.
- Provide a minimal tray icon or window with: host address field, connect/disconnect button, status indicator.

### 5.2 Host (PC B)
- Run a WebSocket server on a configurable port (default `8765`).
- Accept exactly one sender connection at a time.
- Render a transparent, always-on-top, click-through overlay window covering the primary display.
- Draw a clearly distinguishable cursor sprite (different color/shape from the system cursor) at the received normalized coordinates, scaled to the host's primary display.
- Hide the mirrored cursor when no sender is connected or when no update has arrived for > 500 ms.
- Provide a minimal tray icon or window with: listening port, connection status, exit button.

### 5.3 Wire Protocol
- Transport: WebSocket over TCP.
- Message format (v1): JSON text frames, one position per frame.
  ```json
  { "x": 0.5123, "y": 0.7842, "t": 1714680000123 }
  ```
- Future: optional binary packed frames for lower overhead.

## 6. Non-Functional Requirements

- **Latency:** end-to-end (sender mouse move → host overlay redraw) under 30 ms on the same LAN at 120 Hz update rate.
- **CPU:** sender < 2% CPU on a modern desktop; host overlay < 3% CPU when idle-rendering at 60 fps.
- **Stability:** must survive sender disconnects, host restarts, and display sleep without requiring a manual restart of the still-running side.
- **Visual:** mirrored cursor must remain visible over fullscreen-borderless League of Legends. Exclusive fullscreen is out of scope (overlay won't render over true exclusive fullscreen).

## 7. Technical Approach (proposed, not locked in)

- **Language / runtime:** Python with `websockets` for the network layer is the simplest start. For the overlay, options are:
  - **Tkinter / PyQt6** transparent always-on-top window with click-through via Win32 `SetWindowLong` (`WS_EX_LAYERED | WS_EX_TRANSPARENT`).
  - **Electron** or **Tauri** if a richer UI is desired later.
- **Cursor capture (sender):** `GetCursorPos` via `ctypes`/`pywin32`, polled on a timer.
- **Coordinate mapping:** normalize on send, denormalize on receive against each side's primary monitor resolution. v1 assumes single-monitor setups on both ends.
- **Packaging:** PyInstaller one-file executables for `akimbo-sender.exe` and `akimbo-host.exe`.

## 8. Open Questions

- Does League of Legends' anti-cheat (Vanguard) tolerate a transparent click-through overlay drawn by a third-party process? Needs empirical testing; this is the highest-risk unknown.
- Multi-monitor handling on either side — defer to v2 or solve up front?
- Should the sender display its own preview of where the cursor *would* be on the host's resolution (helps the player on PC A know what PC B sees)?
- Visual style of the mirrored cursor — solid colored arrow, ring, crosshair? Probably worth a quick A/B during playtesting.

## 9. Milestones

1. **M1 — Echo:** sender streams position, host logs it. No overlay yet.
2. **M2 — Overlay:** host draws the mirrored cursor on a transparent always-on-top window.
3. **M3 — Click-through:** overlay does not steal focus or block input under it.
4. **M4 — Game test:** verify the overlay renders over League in borderless fullscreen and the latency feels playable.
5. **M5 — Polish:** tray UIs, auto-reconnect, packaged executables.
