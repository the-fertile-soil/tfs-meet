"""Start a TFS meeting as its moderator with no login.

  POST /api/start   {name, email, kind: "video"|"audio", title?, others?, room?}
                    returns {"url": ...}, a Jitsi link that carries a short-lived signed token

The token makes the visitor the moderator of exactly one room. Everyone else
joins that room as a guest with the plain link. Capacity rules are enforced here,
because this is the only way to get a moderator token.
"""
import base64
import hashlib
import hmac
import json
import re
import secrets
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

import availability as av
import limits

CFG = Path("/opt/tfs-review/jwt.json")
HOSTS = Path("/opt/tfs-review/hosts")
PENDING = Path("/opt/tfs-review/pending-starts.json")
SCHEDULES = Path("/opt/tfs-review/schedules")
PUBLIC = "https://meet.example.org"
HOLD = 120          # seconds a freshly issued room stays reserved while the host connects
PER_HOUR = 6        # starts per IP per hour
TOKEN_LIFE = 3 * 3600
AUDIO_HASH = "#config.startAudioOnly=true&config.startWithVideoMuted=true"
NOCACHE = {"Cache-Control": "no-store"}

router = APIRouter()
_hits = {}


def b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def make_jwt(payload: dict) -> str:
    secret = json.loads(CFG.read_text())["secret"].encode()
    head = b64(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    body = b64(json.dumps(payload, separators=(",", ":")).encode())
    sig = hmac.new(secret, (head + "." + body).encode(), hashlib.sha256).digest()
    return head + "." + body + "." + b64(sig)


def load_pending(now: float) -> dict:
    try:
        d = json.loads(PENDING.read_text())
    except Exception:
        d = {}
    return {k: v for k, v in d.items() if now - v < HOLD}


def parse_emails(raw) -> list:
    out, seen = [], set()
    for e in re.split(r"[\s,;]+", str(raw or "")):
        e = e.strip()
        if not e:
            continue
        if not av.EMAIL_RE.match(e) or len(e) > 120:
            raise HTTPException(status_code=400, detail="Not a valid email address: " + e[:60])
        if e.lower() not in seen:
            seen.add(e.lower())
            out.append(e)
    return out


def _instant_html(host_name, what, title, room, link):
    import html as _h
    try:
        tpl = Path("/opt/tfs-review/invite_tpl.html").read_text()
        head = tpl[:tpl.index("<!-- card -->")]
        tail = tpl[tpl.index("<!-- notice -->"):]
    except Exception:
        return ""
    e = _h.escape
    F = "font-family:system-ui,-apple-system,'Segoe UI',Roboto,Arial,sans-serif;"
    card = (
        '<!-- card -->\n<tr><td bgcolor="#fffdf9" style="background:#fffdf9;border-left:1px solid #e6d9c8;border-right:1px solid #e6d9c8;padding:32px 32px 8px 32px;' + F + 'color:#2e2118;">\n'
        '<div style="font-size:12px;letter-spacing:2px;text-transform:uppercase;color:#c0562b;font-weight:700;">You\'re invited!</div>\n'
        '<h1 style="margin:8px 0 6px 0;font-family:Georgia,\'Times New Roman\',serif;font-size:32px;line-height:1.15;color:#2e2118;font-weight:700;">' + e(title) + '</h1>\n'
        '<p style="margin:0 0 24px 0;font-size:16px;line-height:1.55;color:#6b5a4b;">' + e(host_name) + ' has invited you to ' + e(what) + ' on Example Org\'s free meeting service. It is happening now. No account needed.</p>\n'
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin:8px 0 10px 0;width:100%;"><tr><td align="center" bgcolor="#5f7650" style="background:#5f7650;border-radius:999px;">'
        '<a href="' + e(link) + '" style="display:block;padding:16px 24px;' + F + 'font-size:18px;font-weight:700;color:#ffffff;text-decoration:none;border-radius:999px;">Join the meeting</a></td></tr></table>\n'
        '<p style="margin:0 0 20px 0;font-size:13px;line-height:1.5;color:#6b5a4b;text-align:center;word-break:break-all;">Or paste this link into your browser:<br><a href="' + e(link) + '" style="color:#4a5e3d;">' + e(link) + '</a></p>\n'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin:0 0 22px 0;background:#f4eadc;border:1px solid #e6d9c8;border-radius:12px;"><tr><td style="padding:16px 20px;font-family:system-ui,-apple-system,\'Segoe UI\',Roboto,Arial,sans-serif;font-size:14px;line-height:1.6;color:#2e2118;"><div style="font-size:12px;letter-spacing:2px;text-transform:uppercase;color:#c0562b;font-weight:700;">Trouble joining?</div><div style="padding-top:6px;"><strong>1.</strong> Open the link in Chrome or Firefox.<br><strong>2.</strong> Turn off content blockers for meet.example.org.<br><strong>3.</strong> Allow camera and microphone when asked.</div><div style="padding-top:8px;color:#6b5a4b;font-size:13px;">Safari with strict privacy settings is the most common cause.</div></td></tr></table>\n'
    )
    out = head + card + tail
    for k, v in {"{{TITLE}}": e(title), "{{SHORT}}": e(host_name) + " invited you to a meeting", "{{ROOM}}": e(room), "{{MANAGE}}": ""}.items():
        out = out.replace(k, v)
    return out


def send_invites(guests, host_name, title, room, kind):
    try:
        import schedule as sc
    except Exception:
        return
    link = PUBLIC + "/" + room
    what = "an audio-only meeting" if kind == "audio" else "a video meeting"
    body = (
        "Hello,\n\n"
        + host_name + " has invited you to " + what + " on Example Org's free meeting service: " + title + "\n\n"
        "Join here: " + link + "\n\n"
        "Please note: this meeting is recorded, transcribed, and summarized by AI software on "
        "Example Org's own server. By joining, you agree to be recorded. When the meeting is over, "
        "everyone invited gets an email with a private link to the summary and the saved files. "
        "Recordings that are not saved are deleted automatically after 30 days.\n\n"
        "Questions: " + sc.REPLY_TO + "\n\nExample Org\n"
    )
    html_ = _instant_html(host_name, what, title, room, link)
    for g in guests:
        try:
            sc.send(g, "You are invited: " + title, body, html=html_)
        except Exception:
            pass


def err(code: int, msg: str, **extra):
    return JSONResponse({"detail": msg, **extra}, status_code=code, headers=NOCACHE)


@router.post("/api/start")
async def start(request: Request):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Bad request")
    if body.get("website"):
        raise HTTPException(status_code=400, detail="Bad request")
    name = re.sub(r"\s+", " ", str(body.get("name", ""))).strip()[:60]
    email = str(body.get("email", "")).strip()
    kind = body.get("kind")
    title = re.sub(r"\s+", " ", str(body.get("title", ""))).strip()[:80]
    if not name:
        raise HTTPException(status_code=400, detail="Please enter your name")
    if not av.EMAIL_RE.match(email) or len(email) > 120:
        raise HTTPException(status_code=400, detail="Please enter a valid email address")
    if kind not in ("video", "audio"):
        raise HTTPException(status_code=400, detail="Choose video or audio-only")
    others = parse_emails(body.get("others"))
    if len(others) > 7:
        raise HTTPException(status_code=400, detail="You can add up to 7 additional email addresses")

    now = time.time()
    ip = (request.headers.get("x-forwarded-for") or (request.client.host if request.client else "x")).split(",")[0].strip()
    import access as _acc
    if _acc.denied(email, ip):
        return err(403, "We are not able to start meetings for this address. Questions: meetings-admin@example.org")
    hits = [t for t in _hits.get(ip, []) if now - t < 3600]
    if len(hits) >= PER_HOUR:
        return err(429, "Too many meetings started from this connection. Please try again later.")

    st = av.status()
    pending = load_pending(now)
    sched = None
    want = re.sub(r"[^a-z0-9-]", "", str(body.get("room", "")).lower())
    if want:
        p = SCHEDULES / (want + ".json")
        try:
            sched = json.loads(p.read_text())
        except Exception:
            return err(404, "That scheduled meeting was not found.")
        if sched.get("cancelled"):
            return err(404, "That meeting was cancelled.")
        if email.lower() not in [a.lower() for a in sched.get("attendees", [])]:
            return err(403, "That email address is not on the invitation for this meeting.")
        s = datetime.strptime(sched["start_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
        if now < s - 15 * 60:
            return err(409, "This meeting opens 15 minutes before its start time.")
        if now > s + av.LIMIT + 600:
            return err(409, "This meeting's time has passed.")
        room, kind = want, "video"
    elif kind == "video":
        live = set(st["video"].get("live_rooms", []))
        busy_pending = [k for k in pending if not k.startswith("audio-") and k not in live]
        if (st["known"] and not st["video"]["available"]) or (st["video"].get("in_use", 0) + len(busy_pending) >= av.MAX_VIDEO):
            return err(409, "Both video sessions are in use right now.", free_at=st["video"]["free_at"])
        room = "tfs-" + "".join(secrets.choice("abcdefghjkmnpqrstuvwxyz23456789") for _ in range(6))
    else:
        used = {r["room"] for r in st["audio"]["rooms"] if r["in_use"]}
        free = [r for r in av.AUDIO_ROOMS if r not in used and r not in pending]
        if not free or (st["known"] and st["audio"]["free"] - len([k for k in pending if k.startswith("audio-") and k not in used]) <= 0):
            return err(409, "All of the audio-only sessions are in use right now.")
        room = free[0]

    pending[room] = now
    PENDING.write_text(json.dumps(pending))
    hits.append(now)
    _hits[ip] = hits

    if sched is None:
        HOSTS.mkdir(parents=True, exist_ok=True)
        attendees = [email] + [o for o in others if o.lower() != email.lower()]
        meta = {"room": room, "title": title or ("TFS meeting " + datetime.now(timezone.utc).strftime("%b %d")),
                "host_name": name, "host_email": email, "attendees": attendees, "kind": kind,
                "created": now}
        (HOSTS / (room + ".json")).write_text(json.dumps(meta, indent=2))

    if sched is None:
        limits.set_room(room, limits.START_MIN)

    if sched is None:
        guests = [o for o in others if o.lower() != email.lower()]
        if guests:
            threading.Thread(target=send_invites, args=(guests, name, meta["title"], room, kind), daemon=True).start()

    token = make_jwt({
        "iss": "tfs", "aud": "tfs", "sub": "*", "room": room,
        "iat": int(now), "nbf": int(now) - 30, "exp": int(now) + TOKEN_LIFE,
        "moderator": True,
        "context": {"user": {"name": name, "moderator": True, "affiliation": "owner"}},
    })
    url = PUBLIC + "/" + room + "?jwt=" + token
    if kind == "audio":
        url += AUDIO_HASH
    return JSONResponse({"url": url, "room": room, "kind": kind}, headers=NOCACHE)


def verify_jwt(tok: str) -> dict:
    try:
        h, b, sig = tok.split(".")
        secret = json.loads(CFG.read_text())["secret"].encode()
        good = b64(hmac.new(secret, (h + "." + b).encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(good, sig):
            raise ValueError("sig")
        pad = "=" * (-len(b) % 4)
        claims = json.loads(base64.urlsafe_b64decode(b + pad))
        if claims.get("iss") != "tfs" or claims.get("exp", 0) < time.time():
            raise ValueError("claims")
        return claims
    except Exception:
        raise HTTPException(status_code=403, detail="Only the host can add time.")


def clock(room: str) -> dict:
    now = time.time()
    started = None
    try:
        d = json.loads(av.STATE.read_text())
        for r in d.get("rooms", []):
            if r["room"] == room:
                started = r["started"]
    except Exception:
        pass
    minutes = limits.minutes_for(room)
    if started is None:
        return {"known": False, "minutes": minutes}
    elapsed = int(now - started)
    return {"known": True, "minutes": minutes, "elapsed": elapsed, "remaining": minutes * 60 - elapsed}


@router.get("/api/room-clock")
async def room_clock(room: str = ""):
    room = re.sub(r"[^a-z0-9-]", "", room.lower())[:60]
    return JSONResponse(clock(room), headers=NOCACHE)


@router.post("/api/extend")
async def extend(request: Request):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Bad request")
    claims = verify_jwt(str(body.get("jwt", "")))
    room = str(claims.get("room", ""))
    c = clock(room)
    if not c["known"]:
        return err(409, "This meeting is not running.")
    if c["minutes"] >= limits.MAX_MIN:
        return err(409, "This meeting is already at the 90 minute limit.")
    if c["remaining"] > 16 * 60:
        return err(409, "It is too early to add time. Try again in the last 15 minutes.")
    if c["remaining"] < -5:
        return err(409, "This meeting has already ended.")
    limits.set_room_keep(room, min(limits.MAX_MIN, c["minutes"] + limits.STEP_MIN))
    return JSONResponse(clock(room), headers=NOCACHE)


@router.get("/api/room-title")
async def room_title(room: str = ""):
    room = re.sub(r"[^a-z0-9-]", "", room.lower())[:60]
    title = ""
    try:
        hm = json.loads((HOSTS / (room + ".json")).read_text())
        if 0 <= time.time() - hm.get("created", 0) < 8 * 3600:
            title = hm.get("title", "")
    except Exception:
        pass
    if not title:
        try:
            sm = json.loads((SCHEDULES / (room + ".json")).read_text())
            if not sm.get("cancelled"):
                title = sm.get("title", "")
        except Exception:
            pass
    return JSONResponse({"title": title}, headers=NOCACHE)
