import json
import struct
import time


def encode_position(x: float, y: float) -> str:
    return json.dumps({"x": x, "y": y, "t": int(time.time() * 1000)})


def decode_position(payload: str) -> tuple[float, float, int]:
    data = json.loads(payload)
    return float(data["x"]), float(data["y"]), int(data["t"])


# Image frames are binary: 4 little-endian float32 (dest_x, dest_y, dest_w, dest_h
# normalized 0..1 of host screen) followed by raw JPEG bytes.
_IMAGE_HEADER = struct.Struct("<ffff")


def encode_image(jpeg: bytes, dest: tuple[float, float, float, float]) -> bytes:
    return _IMAGE_HEADER.pack(*dest) + jpeg


def decode_image(payload: bytes) -> tuple[bytes, tuple[float, float, float, float]]:
    dest = _IMAGE_HEADER.unpack_from(payload, 0)
    return bytes(payload[_IMAGE_HEADER.size:]), dest
