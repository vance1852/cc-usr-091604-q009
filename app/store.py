"""JSON 快照持久化：原子写入，避免半截文件。"""

import json
import os

from app.codec import decode, encode
from app.models import TravelState

SNAPSHOT_VERSION = 1


def save_snapshot(state: TravelState, path: str) -> str:
    payload = {"version": SNAPSHOT_VERSION, "state": encode(state)}
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp_path, path)
    return path


def load_snapshot(path: str) -> TravelState:
    with open(path, "r", encoding="utf-8") as fh:
        payload = json.load(fh)
    if payload.get("version") != SNAPSHOT_VERSION:
        raise ValueError(f"不支持的快照版本: {payload.get('version')!r}")
    state = decode(payload["state"])
    if not isinstance(state, TravelState):
        raise ValueError("快照内容不是有效的行程状态")
    return state
