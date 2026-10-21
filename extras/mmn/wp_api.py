"""Scheduling API used by the Example Org WordPress plugin. Only callers holding the shared key get in."""
import hmac
import json
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

import schedule as sch

KEY = Path("/opt/mmn-review/wp.key")
router = APIRouter(prefix="/api/wp")
NOCACHE = {"Cache-Control": "no-store"}


def _auth(request: Request) -> None:
    try:
        good = KEY.read_text().strip()
    except Exception:
        raise HTTPException(status_code=503, detail="Not configured")
    got = request.headers.get("x-mmn-key", "")
    if not good or not hmac.compare_digest(good, got):
        raise HTTPException(status_code=403, detail="Forbidden")


@router.post("/schedule")
async def schedule(request: Request):
    _auth(request)
    return await sch.create(request)


@router.get("/list")
async def listing(request: Request):
    _auth(request)
    out = []
    import secrets
    for m in sch.load_all():
        if m.get("cancelled"):
            continue
        if not m.get("share_token"):
            m["share_token"] = secrets.token_urlsafe(9)
            try:
                (sch.SCHEDULES / (m["room"] + ".json")).write_text(json.dumps(m, indent=2))
            except Exception:
                pass
        out.append({"room": m["room"], "title": m.get("title", ""), "start_utc": m["start_utc"], "tz": m.get("tz", ""),
                    "when": sch.fmt_local(m["start_utc"], m.get("tz") or "America/Los_Angeles"), "host": m.get("host", ""),
                    "attendees": m.get("attendees", []), "url": sch.PUBLIC_BASE + "/?room=" + m["room"] + (("&k=" + m["share_token"]) if m.get("share_token") else "")})
    out.sort(key=lambda x: x["start_utc"])
    return JSONResponse({"meetings": out}, headers=NOCACHE)


@router.post("/cancel/{room}")
async def cancel(room: str, request: Request):
    _auth(request)
    return await sch.cancel(room, request)
