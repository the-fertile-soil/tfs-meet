"""Move finished Jibri recordings into the TFS review pipeline (storage box uploads dir)."""
import json
import re
import shutil
import time
from pathlib import Path

SRCS = [Path("/opt/jitsi-cfg/jibri/recordings")] + [Path("/opt/jitsi-cfg/jibri%d/recordings" % i) for i in range(1, 5)]
UPLOADS = Path("/opt/tfs-review/uploads")

DIRS = []
for SRC in SRCS:
    if SRC.exists():
        DIRS += sorted(SRC.iterdir())
for d in DIRS:
    if not d.is_dir() or not (d / ".done").exists():
        continue
    vids = sorted(d.glob("*.mp4"))
    if not vids:
        continue
    v = vids[0]
    room = re.sub(r"[^A-Za-z0-9._-]", "_", v.stem.split("_")[0]) or "meeting"
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    dest = UPLOADS / (stamp + "_" + room + ".mp4")
    part = UPLOADS / (dest.name + ".part")
    shutil.copyfile(v, part)
    part.rename(dest)
    recipients = []
    host = ""
    sched = Path("/opt/tfs-review/schedules") / (room + ".json")
    title = "Meeting " + room
    if sched.exists():
        try:
            sm = json.loads(sched.read_text())
            if not sm.get("cancelled"):
                recipients = list(sm.get("attendees", []))
                title = sm.get("title") or title
        except Exception:
            pass
    if not recipients:
        hp = Path("/opt/tfs-review/hosts") / (room + ".json")
        try:
            hm = json.loads(hp.read_text())
            if 0 <= time.time() - hm.get("created", 0) < 8 * 3600:
                recipients = list(hm.get("attendees", []))
                title = hm.get("title") or title
                host = hm.get("host_email", "")
        except Exception:
            pass
    if sched.exists():
        try:
            host = json.loads(sched.read_text()).get("host", "") or host
        except Exception:
            pass
    meta = {
        "file": dest.name,
        "title": title,
        "bytes": dest.stat().st_size,
        "uploaded_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "recipients": recipients,
        "host": host,
        "keep": False,
        "status": "uploaded",
        "source": "jibri",
        "kind": "audio" if room.startswith("audio-") else "video",
    }
    dest.with_name(dest.name + ".json").write_text(json.dumps(meta, indent=2))
    try:
        import sys
        sys.path.insert(0, "/opt/tfs-review")
        import access
        who = host
        if sched.exists():
            try:
                who = json.loads(sched.read_text()).get("host", "") or who
            except Exception:
                pass
        if who:
            access.record_success(who)
    except Exception as exc:
        print("allow list update failed:", exc)
    shutil.rmtree(d)
    print("ingested", dest.name)
