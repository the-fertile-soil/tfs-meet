"""Nightly cleanup: delete unsaved meetings older than the retention setting.

Usage: purge.py [--dry-run]
Saved meetings (keep = true) are never touched. Meetings still being processed are skipped.
Every deletion is appended to /opt/tfs-review/purge.log.
"""
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

UPLOADS = Path("/opt/tfs-review/uploads")
CONFIG = Path("/opt/tfs-review/config.json")
LOG = Path("/opt/tfs-review/purge.log")
BUSY = ("transcribing", "summarizing")


def retention_days() -> int:
    try:
        d = int(json.loads(CONFIG.read_text()).get("retention_days", 30))
    except Exception:
        d = 30
    return min(max(d, 1), 365)


def main() -> int:
    dry = "--dry-run" in sys.argv
    days = retention_days()
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    deleted = 0
    for p in sorted(UPLOADS.glob("*.json")):
        try:
            m = json.loads(p.read_text())
            up = datetime.strptime(m["uploaded_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if m.get("keep") or m.get("status") in BUSY or up > cutoff:
            continue
        files = [f for f in UPLOADS.glob(m["file"] + "*") if f.is_file()]
        line = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) + (" WOULD DELETE " if dry else " DELETED ") \
            + m["file"] + " (" + str(len(files)) + " files, uploaded " + m["uploaded_utc"] + ", retention " + str(days) + "d)"
        print(line)
        if not dry:
            for f in files:
                f.unlink()
            try:
                sys.path.insert(0, "/opt/tfs-review")
                import voices
                voices.forget_meeting(m["file"])
            except Exception:
                pass
            with open(LOG, "a") as fh:
                fh.write(line + "\n")
        deleted += 1
    print("retention " + str(days) + " days, " + str(deleted) + (" would be deleted" if dry else " deleted"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
