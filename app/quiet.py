"""Is any TFS meeting running (or about to)? Heavy background work must wait while this is True."""
import json
import time
from datetime import datetime, timezone
from pathlib import Path

STATE = Path("/opt/jitsi-cfg/prosody/config/tfs-rooms.json")
PENDING = Path("/opt/tfs-review/pending-starts.json")
SCHEDULES = Path("/opt/tfs-review/schedules")
LEAD = 15 * 60      # scheduled meetings open 15 minutes early
LENGTH = 5400


def meetings_live(now=None) -> bool:
    now = now or time.time()
    try:
        d = json.loads(STATE.read_text())
        if now - d.get("updated", 0) < 120 and d.get("rooms"):
            return True
    except Exception:
        pass
    try:
        if any(now - v < 180 for v in json.loads(PENDING.read_text()).values()):
            return True
    except Exception:
        pass
    for p in SCHEDULES.glob("tfs-*.json"):
        try:
            m = json.loads(p.read_text())
            if m.get("cancelled"):
                continue
            s = datetime.strptime(m["start_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
            if s - LEAD <= now < s + LENGTH:
                return True
        except Exception:
            continue
    return False


if __name__ == "__main__":
    print("live" if meetings_live() else "quiet")
