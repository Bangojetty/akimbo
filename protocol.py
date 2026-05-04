import json
import time


def encode_position(x: float, y: float, default_cursor: bool, rmb: bool) -> str:
    return json.dumps({
        "x": x,
        "y": y,
        "t": int(time.time() * 1000),
        "d": default_cursor,
        "r": rmb,
    })


def decode_position(payload: str) -> tuple[float, float, int, bool, bool]:
    data = json.loads(payload)
    return (
        float(data["x"]),
        float(data["y"]),
        int(data["t"]),
        bool(data.get("d", True)),
        bool(data.get("r", False)),
    )


# Image frames are raw JPEG bytes. Size is encoded in the JPEG header (read by
# Pillow on the host); position on the host overlay is owned by the host UI.
def encode_image(jpeg: bytes) -> bytes:
    return jpeg


def decode_image(payload: bytes) -> bytes:
    return bytes(payload)
