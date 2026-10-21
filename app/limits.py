"""Per-room time limits shared with the Prosody time-limit module.

Walk-in meetings start at 30 minutes and can be extended in 30 minute steps
up to 90. Rooms with no entry (scheduled meetings) get the full 90 minutes.
"""
import json
import os
import time
from pathlib import Path

LIM = Path("/opt/jitsi-cfg/prosody/config/tfs-limits.json")
DEFAULT_MIN = 90
START_MIN = 30
STEP_MIN = 30
MAX_MIN = 90


def load() -> dict:
    try:
        return json.loads(LIM.read_text())
    except Exception:
        return {}


def save(d: dict) -> None:
    tmp = LIM.with_name(LIM.name + ".tmp")
    tmp.write_text(json.dumps(d))
    os.chmod(tmp, 0o644)
    tmp.rename(LIM)


def set_room(room: str, minutes: int) -> None:
    d = load()
    now = time.time()
    d = {k: v for k, v in d.items() if now - v.get("set", 0) < 6 * 3600}
    d[room] = {"minutes": minutes, "set": now}
    save(d)


def set_room_keep(room: str, minutes: int) -> None:
    """Change the limit but keep the original set time."""
    d = load()
    e = d.get(room, {"set": time.time()})
    e["minutes"] = minutes
    d[room] = e
    save(d)


def minutes_for(room: str) -> int:
    return int(load().get(room, {}).get("minutes", DEFAULT_MIN))
