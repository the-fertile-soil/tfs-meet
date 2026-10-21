"""Immediate post-meeting report to the host (works before any transcript or summary exists)."""
import html as _h
import json
import subprocess
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import notify

PRES = Path("/opt/mmn-jitsi-cfg/prosody/config/mmn-presence.jsonl")
TZ = ZoneInfo("America/Los_Angeles")


def media_seconds(path: Path) -> float:
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                             capture_output=True, text=True, timeout=60).stdout.strip()
        return float(out)
    except Exception:
        return 0.0


def presence(room: str, since: float) -> list:
    """Per-person [{name,email,first,last,secs}] from the attendance log for events after `since`."""
    people, open_ids = {}, {}
    if not PRES.exists():
        return []
    for line in PRES.read_text().splitlines():
        try:
            e = json.loads(line)
        except Exception:
            continue
        if e.get("room") != room or e["t"] < since:
            continue
        key = (e.get("email") or e.get("name") or e.get("id")).lower()
        p = people.setdefault(key, {"name": e.get("name", ""), "email": e.get("email", ""),
                                    "first": e["t"], "last": e["t"], "secs": 0})
        if e.get("name"):
            p["name"] = e["name"]
        if e["ev"] == "join":
            open_ids[e["id"]] = e["t"]
        else:
            st = open_ids.pop(e["id"], None)
            if st:
                p["secs"] += e["t"] - st
            p["last"] = e["t"]
    return list(people.values())


def fmt_dur(sec: float) -> str:
    m = int(round(sec / 60))
    return "under a minute" if sec < 45 else (str(m) + " minute" + ("" if m == 1 else "s"))


def fmt_when(ts: float) -> str:
    d = datetime.fromtimestamp(ts, TZ)
    return d.strftime("%A, %B ") + str(d.day) + d.strftime(", %Y at ") + str(int(d.strftime("%I"))) + d.strftime(":%M %p %Z")


def trim(days: int = 15) -> None:
    """Drop attendance lines older than the retention window."""
    import time
    try:
        cut = time.time() - days * 86400
        keep = [l for l in PRES.read_text().splitlines() if json.loads(l).get("t", 0) >= cut]
        PRES.write_text("\n".join(keep) + ("\n" if keep else ""))
    except Exception:
        pass


def build(meta: dict, media: Path, room: str) -> dict:
    trim()
    secs = media_seconds(media)
    end = media.stat().st_mtime
    start = end - secs
    ppl = presence(room, start - 900)
    if ppl:
        start = min(p["first"] for p in ppl)
        end = max(p["last"] for p in ppl)
        if end > start:
            secs = end - start
    meta["session"] = {"start": int(start), "end": int(end), "seconds": int(secs),
                       "people": [{"name": p["name"], "email": p["email"], "minutes": round(p["secs"] / 60)} for p in ppl]}
    return meta["session"]


def send(meta: dict, host_token: str) -> None:
    s = meta["session"]
    host = meta["host"]
    base = notify.PUBLIC_BASE + "/review/" + host_token
    title = meta.get("title") or "your meeting"
    if s["people"]:
        who = [(p["name"] or p["email"]) + ((" (" + p["email"] + ")") if p["name"] and p["email"] else "") for p in s["people"]]
    else:
        who = [x + " (invited)" for x in (meta.get("recipients") or [])]
    when = fmt_when(s["start"])
    dur = fmt_dur(s["seconds"])
    lis = "".join('<li style="margin:3px 0;">' + _h.escape(x) + "</li>" for x in who) or "<li>No attendance was recorded.</li>"
    P = 'style="margin:0 0 6px 0;font-size:15px;line-height:1.55;color:#241a4a;"'
    inner = (
        '<h1 style="margin:0 0 6px 0;font-size:24px;color:#241a4a;">Your meeting has ended</h1>'
        '<p style="margin:0 0 14px 0;font-size:16px;color:#241a4a;"><strong>' + _h.escape(title) + "</strong></p>"
        "<p " + P + "><strong>When:</strong> " + _h.escape(when) + "</p>"
        "<p " + P + "><strong>Duration:</strong> " + _h.escape(dur) + "</p>"
        "<p " + P + "><strong>Who was there:</strong></p>"
        '<ul style="margin:0 0 16px 0;padding-left:22px;font-size:15px;color:#241a4a;">' + lis + "</ul>"
        + notify._button("Review all meeting data", base)
        + notify._button("Watch the video", base + "/watch", primary=False)
        + '<p style="margin:14px 0 0 0;font-size:14px;line-height:1.55;color:#5b5280;">The transcript and summary are being prepared. '
          "The review link already works and fills in as they are ready. Everything is deleted automatically after 15 days.</p>"
    )
    text = ("Your meeting has ended: " + title + "\nWhen: " + when + "\nDuration: " + dur + "\nWho was there:\n  "
            + "\n  ".join(who) + "\n\nReview all meeting data: " + base + "\nWatch the video: " + base + "/watch\n\n"
            "The transcript and summary are being prepared; the review link fills in as they are ready. "
            "Everything is deleted automatically after 15 days.\n")
    notify._send(host, "Meeting report: " + title, text, notify._frame("Meeting report", inner))
