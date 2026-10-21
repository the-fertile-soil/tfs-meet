"""Live availability for TFS meetings, plus a small email waitlist.

  GET  /api/availability   public JSON status (video slot, audio rooms)
  POST /api/waitlist       {"email": "...", "kind": "video" | "audio"}
"""
import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

STATE = Path("/opt/jitsi-cfg/prosody/config/tfs-rooms.json")
SCHEDULES = Path("/opt/tfs-review/schedules")
WAITLIST = Path("/opt/tfs-review/waitlist.json")
LIMIT = 5400
AUDIO_ROOMS = ["audio-1", "audio-2", "audio-3", "audio-4"]
MAX_VIDEO = 2          # simultaneous video meetings
MAX_PEOPLE = 8         # people per meeting
RECORDERS = 5          # Jibri recorders: every meeting, video or audio, needs one
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
CORS = {"Access-Control-Allow-Origin": "https://example.org", "Cache-Control": "no-store"}

router = APIRouter()


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def reservations(now: float) -> list:
    """Active or upcoming reservations as (start_ts, room), sorted by start."""
    out = []
    for p in SCHEDULES.glob("tfs-*.json"):
        try:
            m = json.loads(p.read_text())
            if m.get("cancelled"):
                continue
            s = datetime.strptime(m["start_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
            if s + LIMIT > now:
                out.append((s, m.get("room", "")))
        except Exception:
            pass
    return sorted(out)


def status() -> dict:
    import limits
    now = time.time()
    rooms, fresh = [], False
    try:
        d = json.loads(STATE.read_text())
        fresh = now - d.get("updated", 0) < 90
        rooms = d.get("rooms", [])
    except Exception:
        pass
    video_rooms = [r for r in rooms if not r["room"].startswith("audio-")]
    live_names = {r["room"] for r in video_rooms}
    audio_used = {r["room"] for r in rooms if r["room"] in AUDIO_ROOMS}
    res = reservations(now)
    # every video meeting holds a slot until its end: live rooms, plus reservations whose hour has begun but whose room is not live yet
    ends = [r["started"] + limits.minutes_for(r["room"]) * 60 for r in video_rooms]
    ends += [s + LIMIT for s, room in res if s <= now < s + LIMIT and room not in live_names]
    # reservations starting within 30 minutes also hold a slot, so a walk-in cannot collide with them
    soon = [(s, room) for s, room in res if now < s < now + 1800 and room not in live_names]
    held = len(ends) + len(soon)
    video_free = held < MAX_VIDEO
    busy_until = None
    if not video_free:
        cand = sorted(ends + [s + LIMIT for s, _ in soon])
        busy_until = cand[held - MAX_VIDEO] if len(cand) > held - MAX_VIDEO else cand[0]
    next_res = next((s for s, _ in res if s > now), None)
    audio_cap = max(0, min(len(AUDIO_ROOMS), RECORDERS - len(ends)))  # slots left for audio while video meetings run
    audio_free = max(0, audio_cap - len(audio_used))
    return {
        "known": fresh,
        "video": {
            "available": video_free,
            "free_at": None if video_free else iso(busy_until),
            "next_reservation": iso(next_res) if next_res else None,
            "in_use": len(ends),
            "max": MAX_VIDEO,
            "live_rooms": sorted(live_names),
        },
        "audio": {
            "total": len(AUDIO_ROOMS),
            "free": audio_free,
            "rooms": [{"room": r, "in_use": r in audio_used} for r in AUDIO_ROOMS],
        },
        "now": iso(now),
    }


@router.get("/api/availability")
async def availability():
    return JSONResponse(status(), headers=CORS)


@router.options("/api/waitlist")
async def waitlist_options():
    return JSONResponse({}, headers={**CORS, "Access-Control-Allow-Methods": "POST, OPTIONS",
                                     "Access-Control-Allow-Headers": "Content-Type"})


@router.post("/api/waitlist")
async def waitlist_add(request: Request):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Bad request")
    email = str(body.get("email", "")).strip().lower()
    kind = body.get("kind")
    if kind not in ("video", "audio") or not EMAIL_RE.match(email) or len(email) > 120:
        raise HTTPException(status_code=400, detail="Please enter a valid email address")
    try:
        items = json.loads(WAITLIST.read_text())
    except Exception:
        items = []
    now = time.time()
    items = [i for i in items if now - i["added"] < 86400]
    if len(items) >= 50:
        raise HTTPException(status_code=429, detail="The waitlist is full right now. Please try again later.")
    if not any(i["email"] == email and i["kind"] == kind for i in items):
        items.append({"email": email, "kind": kind, "added": now})
    WAITLIST.write_text(json.dumps(items))
    return JSONResponse({"ok": True}, headers=CORS)
