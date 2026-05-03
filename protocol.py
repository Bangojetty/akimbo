import json
import time


def encode_position(x: float, y: float) -> str:
    return json.dumps({"x": x, "y": y, "t": int(time.time() * 1000)})


def decode_position(payload: str) -> tuple[float, float, int]:
    data = json.loads(payload)
    return float(data["x"]), float(data["y"]), int(data["t"])
