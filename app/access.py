"""Who may schedule meetings for later, and who is blocked.

Public flow (no password):
  POST /api/schedule-request   host on the allow list  -> booked at once, invitations sent
                               host not on the list    -> confirmation email; nothing is booked until the host clicks
  GET  /api/confirm/{token}    summary page with a Confirm button (a button, so mail scanners cannot confirm by prefetching)
  POST /api/confirm/{token}    books the meeting and sends the invitations

Allow list: hosts whose meeting actually took place (a recording was ingested). Added automatically by ingest_jibri.py.
Deny list: emails, whole domains ("@example.com") or IP addresses, managed by moderators at /submit/access.
"""
import html as _html
import json
import re
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

import schedule as sch
import theme

STORE = Path("/opt/tfs-review/access.json")
PENDING_DIR = Path("/opt/tfs-review/pending-schedules")
PENDING_LIFE = 24 * 3600
MAX_PENDING_PER_HOST = 3
REQUESTS_PER_IP_HOUR = 5
MAX_EXTRA = 7
NOCACHE = {"Cache-Control": "no-store", "X-Robots-Tag": "noindex, nofollow, noarchive", "Referrer-Policy": "no-referrer"}
router = APIRouter()
_lock = threading.Lock()
_hits = {}


# ---------- store ----------
def _load() -> dict:
    try:
        d = json.loads(STORE.read_text())
    except Exception:
        d = {}
    d.setdefault("allow", {})
    d.setdefault("deny", {})
    return d


def _save(d: dict) -> None:
    tmp = STORE.with_name(STORE.name + ".tmp")
    tmp.write_text(json.dumps(d, indent=2, sort_keys=True))
    tmp.rename(STORE)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def is_allowed(email: str) -> bool:
    return (email or "").strip().lower() in _load()["allow"]


def denied(email: str = "", ip: str = "") -> bool:
    d = _load()["deny"]
    e = (email or "").strip().lower()
    dom = "@" + e.split("@")[-1] if "@" in e else ""
    return bool((e and e in d) or (dom and dom in d) or (ip and ip.strip().lower() in d))


def record_success(email: str) -> None:
    """Called after a meeting hosted by this address really took place."""
    e = (email or "").strip().lower()
    if not e or not sch.EMAIL_RE.match(e):
        return
    with _lock:
        d = _load()
        if e in d["deny"]:
            return
        row = d["allow"].setdefault(e, {"since": _now_iso(), "meetings": 0, "note": ""})
        row["meetings"] = int(row.get("meetings", 0)) + 1
        row["last"] = _now_iso()
        _save(d)


def add_entry(which: str, value: str, note: str = "") -> None:
    v = (value or "").strip().lower()
    if which not in ("allow", "deny") or not v or len(v) > 120:
        raise HTTPException(status_code=400, detail="Enter an email address, a domain like @example.com, or an IP address")
    ok = bool(sch.EMAIL_RE.match(v)) or (v.startswith("@") and "." in v and " " not in v) or bool(re.match(r"^[0-9a-f:.]+$", v))
    if not ok:
        raise HTTPException(status_code=400, detail="Enter an email address, a domain like @example.com, or an IP address")
    with _lock:
        d = _load()
        if which == "deny":
            d["allow"].pop(v, None)
            d["deny"][v] = {"since": _now_iso(), "note": (note or "")[:200]}
        else:
            d["deny"].pop(v, None)
            d["allow"][v] = {"since": _now_iso(), "meetings": d["allow"].get(v, {}).get("meetings", 0), "note": (note or "")[:200]}
        _save(d)


def remove_entry(which: str, value: str) -> None:
    v = (value or "").strip().lower()
    with _lock:
        d = _load()
        if which in ("allow", "deny"):
            d[which].pop(v, None)
        _save(d)


# ---------- helpers ----------
def _ip(request: Request) -> str:
    return (request.headers.get("x-forwarded-for") or (request.client.host if request.client else "x")).split(",")[0].strip()


def _err(code: int, msg: str):
    return JSONResponse({"detail": msg}, status_code=code, headers=NOCACHE)


def _parse(data: dict, ip: str):
    """Validate a request body. Returns (title, start, tz, host, others) or raises HTTPException."""
    host = str(data.get("host", "")).strip()
    if not sch.EMAIL_RE.match(host) or len(host) > 120:
        raise HTTPException(status_code=400, detail="Please enter a valid email address for the host")
    if denied(host, ip):
        raise HTTPException(status_code=403, detail="We are not able to schedule meetings for this address. Questions: meetings-admin@example.org")
    title = re.sub(r"\s+", " ", str(data.get("title", ""))).strip()[:120] or "TFS meeting"
    tz = str(data.get("tz", ""))
    if tz not in sch.ZONES:
        raise HTTPException(status_code=400, detail="Pick a time zone")
    try:
        local = datetime.strptime(str(data.get("when", "")), "%Y-%m-%dT%H:%M").replace(tzinfo=ZoneInfo(tz))
    except ValueError:
        raise HTTPException(status_code=400, detail="Pick a day on the calendar")
    start = local.astimezone(timezone.utc)
    now = datetime.now(timezone.utc)
    if start < now + timedelta(minutes=5):
        raise HTTPException(status_code=400, detail="Pick a time in the future")
    if start > now + timedelta(days=180):
        raise HTTPException(status_code=400, detail="Pick a date within the next 6 months")
    others, seen = [], {host.lower()}
    for e in re.split(r"[\s,;]+", str(data.get("emails", ""))):
        e = e.strip()
        if not e:
            continue
        if not sch.EMAIL_RE.match(e) or len(e) > 120:
            raise HTTPException(status_code=400, detail="Not a valid email address: " + e[:60])
        if e.lower() in seen:
            continue
        seen.add(e.lower())
        if denied(e):
            continue  # blocked addresses are never emailed
        others.append(e)
    if len(others) > MAX_EXTRA:
        raise HTTPException(status_code=400, detail="You can add up to 7 additional email addresses")
    return title, start, tz, host, others


def _conflict_msg(start):
    m = sch.concurrency_block(start)
    if m:
        return "Two meetings are already booked for that time. Please choose another time."
    return ""


def _book(title, start, tz, host, others) -> dict:
    """Create the schedule file and send the invitations (same record the moderator page creates)."""
    now = datetime.now(timezone.utc)
    attendees = [host] + others
    room = "tfs-" + "".join(secrets.choice("abcdefghjkmnpqrstuvwxyz23456789") for _ in range(6))
    meta = {"room": room, "title": title, "start_utc": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "tz": tz, "host": host, "files": [], "manage_token": secrets.token_urlsafe(18),
            "attendees": attendees, "created_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "invited": [], "via": "public"}
    sch.SCHEDULES.mkdir(parents=True, exist_ok=True)
    path = sch.SCHEDULES / (room + ".json")
    path.write_text(json.dumps(meta, indent=2))
    errors = []
    for a in attendees:
        try:
            sch.send(a, "Invitation: " + title, sch._invite_body_with_files(meta, a.lower() == host.lower()), sch.ics_for(meta),
                     host=host, html=sch.invite_html(meta, a.lower() == host.lower()))
            meta["invited"].append(a)
        except Exception as exc:
            errors.append(a + ": " + str(exc)[:80])
    if errors:
        meta["invite_errors"] = errors
    path.write_text(json.dumps(meta, indent=2))
    return {"room": room, "url": sch.PUBLIC_BASE + "/" + room, "invited": len(meta["invited"]), "errors": errors}


def _clean_pending() -> None:
    PENDING_DIR.mkdir(parents=True, exist_ok=True)
    now = time.time()
    for f in PENDING_DIR.glob("*.json"):
        try:
            if now - json.loads(f.read_text()).get("created", 0) > PENDING_LIFE:
                f.unlink()
        except Exception:
            pass


def _pending_for(host: str) -> int:
    n = 0
    for f in PENDING_DIR.glob("*.json"):
        try:
            if json.loads(f.read_text()).get("host", "").lower() == host.lower():
                n += 1
        except Exception:
            pass
    return n


def _confirm_mail(p: dict, token: str) -> None:
    when = sch.fmt_local(p["start_utc"], p["tz"])
    link = sch.PUBLIC_BASE + "/api/confirm/" + token
    body = ("Please confirm your meeting\n\n"
            "Someone asked to schedule a meeting using this email address as the host. If that was you, confirm it here:\n\n"
            + link + "\n\n"
            "Meeting: " + p["title"] + "\nWhen: " + when + " (up to 90 minutes)\n"
            "People to invite: " + str(len(p["others"])) + "\n\n"
            "Nothing is booked and nobody is emailed until you confirm. This link works for 24 hours. "
            "If this was not you, ignore this message and nothing will happen.\n\nExample Org\n")
    page = ('<div style="font-family:system-ui,Arial,sans-serif;max-width:520px;margin:0 auto;padding:24px;color:#2e2118">'
            '<h2 style="margin:0 0 12px">Please confirm your meeting</h2>'
            '<p>Someone asked to schedule a meeting using this email address as the host. If that was you, confirm it below.</p>'
            '<p style="background:#f4eadc;border-radius:10px;padding:14px"><b>' + sch.esc(p["title"]) + '</b><br>' + sch.esc(when) + '<br>Up to 90 minutes, ' + str(len(p["others"])) + ' other people to invite</p>'
            '<p><a href="' + link + '" style="display:inline-block;background:#4a5e3d;color:#fff;font-weight:700;text-decoration:none;padding:12px 24px;border-radius:999px">Review and confirm</a></p>'
            '<p style="color:#6b5a4b;font-size:14px">Nothing is booked and nobody is emailed until you confirm. The link works for 24 hours. If this was not you, ignore this message.</p></div>')
    sch.send(p["host"], "Confirm your meeting: " + p["title"], body, html=page)


# ---------- public API ----------
@router.post("/api/schedule-request")
async def schedule_request(request: Request):
    try:
        data = await request.json()
    except Exception:
        return _err(400, "Bad request")
    if data.get("website"):
        return JSONResponse({"mode": "confirm"}, headers=NOCACHE)  # honeypot: pretend it worked
    ip = _ip(request)
    now = time.time()
    hits = [t for t in _hits.get(ip, []) if now - t < 3600]
    if len(hits) >= REQUESTS_PER_IP_HOUR:
        return _err(429, "Too many requests from this connection. Please try again later.")
    try:
        title, start, tz, host, others = _parse(data, ip)
    except HTTPException as e:
        return _err(e.status_code, e.detail)
    msg = _conflict_msg(start)
    if msg:
        return _err(409, msg)
    hits.append(now)
    _hits[ip] = hits
    if is_allowed(host):
        res = _book(title, start, tz, host, others)
        return JSONResponse({"mode": "booked", **res}, headers=NOCACHE)
    _clean_pending()
    if _pending_for(host) >= MAX_PENDING_PER_HOST:
        return _err(429, "You already have meetings waiting for confirmation. Please check your email, or try again tomorrow.")
    token = secrets.token_urlsafe(24)
    p = {"title": title, "start_utc": start.strftime("%Y-%m-%dT%H:%M:%SZ"), "tz": tz, "host": host, "others": others,
         "created": now, "ip": ip}
    (PENDING_DIR / (token + ".json")).write_text(json.dumps(p))
    try:
        _confirm_mail(p, token)
    except Exception:
        (PENDING_DIR / (token + ".json")).unlink(missing_ok=True)
        return _err(502, "We could not send the confirmation email. Please check the address and try again.")
    return JSONResponse({"mode": "confirm", "host": host}, headers=NOCACHE)


def _load_pending(token: str):
    if not sch.TOKEN_RE.match(token):
        return None
    f = PENDING_DIR / (token + ".json")
    try:
        p = json.loads(f.read_text())
    except Exception:
        return None
    if time.time() - p.get("created", 0) > PENDING_LIFE:
        f.unlink(missing_ok=True)
        return None
    return p


@router.get("/api/confirm/{token}", response_class=HTMLResponse)
async def confirm_page(token: str):
    p = _load_pending(token)
    if not p:
        body = (theme.top("This link has expired", eyebrow="Scheduling", lead="Confirmation links work for 24 hours, and only once. Please schedule your meeting again.", mode="public") +
                '<div class="card"><p><a class="btn" href="/">Back to the start page</a></p></div>' + theme.FOOT + "</body></html>")
        return HTMLResponse(body, headers=NOCACHE)
    when = sch.fmt_local(p["start_utc"], p["tz"])
    body = (theme.top("Confirm your meeting", eyebrow="Almost done", lead="Check the details, then confirm. Invitations go out as soon as you do.", mode="public", lift=True) +
            '<div class="card lift"><h2>' + sch.esc(p["title"]) + '</h2>'
            '<p><b>When:</b> ' + sch.esc(when) + ' (up to 90 minutes)</p>'
            '<p><b>Host:</b> ' + sch.esc(p["host"]) + '</p>'
            '<p><b>Also invited:</b> ' + (sch.esc(", ".join(p["others"])) if p["others"] else "nobody else yet") + '</p>'
            '<p><button class="btn" id="go" type="button">Confirm and send invitations</button></p><p id="msg" style="font-weight:600"></p></div>'
            '<script>var T=' + json.dumps(token) + ';document.getElementById("go").onclick=function(){var b=this,m=document.getElementById("msg");b.disabled=true;m.textContent="Working...";'
            'fetch("/api/confirm/"+T,{method:"POST",headers:{"X-Requested-With":"tfs"}}).then(function(r){return r.json().catch(function(){return {};}).then(function(d){return {ok:r.ok,d:d};});})'
            '.then(function(x){if(x.ok){m.innerHTML="Confirmed! "+x.d.invited+" invitation(s) sent. Your room link: <a href=\\""+x.d.url+"\\">"+x.d.url+"</a>";b.style.display="none";}'
            'else{b.disabled=false;m.textContent=(x.d&&x.d.detail)||"Something went wrong.";}}).catch(function(){b.disabled=false;m.textContent="Could not reach the server.";});};</script>'
            + theme.FOOT + "</body></html>")
    return HTMLResponse(body, headers=NOCACHE)


@router.post("/api/confirm/{token}")
async def confirm_post(token: str, request: Request):
    if request.headers.get("x-requested-with") != "tfs":
        return _err(403, "Forbidden")
    p = _load_pending(token)
    if not p:
        return _err(404, "This link has expired. Please schedule your meeting again.")
    ip = _ip(request)
    if denied(p["host"], ip):
        return _err(403, "We are not able to schedule meetings for this address. Questions: meetings-admin@example.org")
    start = datetime.strptime(p["start_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    if start < datetime.now(timezone.utc) + timedelta(minutes=5):
        (PENDING_DIR / (token + ".json")).unlink(missing_ok=True)
        return _err(409, "That start time has passed. Please schedule again.")
    msg = _conflict_msg(start)
    if msg:
        return _err(409, msg)
    others = [e for e in p["others"] if not denied(e)]
    (PENDING_DIR / (token + ".json")).unlink(missing_ok=True)  # single use
    res = _book(p["title"], start, p["tz"], p["host"], others)
    return JSONResponse({"ok": True, **res}, headers=NOCACHE)


# ---------- moderator page (behind the meetings password, under /submit/) ----------
def _rows(which: str, d: dict) -> str:
    out = []
    for k, v in sorted(d[which].items()):
        extra = (str(v.get("meetings", 0)) + " meeting(s)") if which == "allow" else sch.esc(v.get("note", ""))
        flip = "deny" if which == "allow" else "allow"
        flip_label = "Block" if which == "allow" else "Allow"
        out.append('<tr><td>' + sch.esc(k) + '</td><td>' + extra + '</td><td>' + sch.esc(v.get("since", "")[:10]) + '</td>'
                   '<td><button class="sm" data-act="move" data-from="' + which + '" data-to="' + flip + '" data-v="' + sch.esc(k) + '">' + flip_label + '</button> '
                   '<button class="sm dg" data-act="remove" data-from="' + which + '" data-v="' + sch.esc(k) + '">Remove</button></td></tr>')
    return "".join(out) or '<tr><td colspan="4" class="note">Nobody yet.</td></tr>'


@router.get("/submit/access", response_class=HTMLResponse)
async def access_page():
    d = _load()
    css = (".sm{padding:5px 12px;font-size:.85rem;margin:0}.dg{background:#a02020}.fr{display:grid;grid-template-columns:2fr 2fr auto;gap:10px;align-items:end}"
           "@media(max-width:700px){.fr{grid-template-columns:1fr}}")
    body = (theme.top("Access list", eyebrow="Moderator area",
                      lead="Anyone on the allow list schedules meetings with no confirmation step. Everyone else confirms by email first. Blocked addresses cannot schedule, start, or be invited.",
                      active="access", lift=True, extra_css=css) +
            '<div class="card lift"><h2>Add someone</h2><div class="fr">'
            '<label>Email, @domain.com, or IP address<input id="v" placeholder="name@example.org"></label>'
            '<label>Note (optional)<input id="n" maxlength="200"></label>'
            '<div><button id="aa" type="button">Allow</button> <button id="ad" type="button" class="dg">Block</button></div></div><p id="msg"></p></div>'
            '<div class="card"><h2>Allow list</h2><p class="note">Hosts whose meetings actually took place are added here automatically.</p>'
            '<div class="tablewrap"><table><tr><th>Address</th><th>History</th><th>Since</th><th></th></tr>' + _rows("allow", d) + '</table></div></div>'
            '<div class="card"><h2>Block list</h2><div class="tablewrap"><table><tr><th>Address</th><th>Note</th><th>Since</th><th></th></tr>' + _rows("deny", d) + '</table></div></div>'
            '<script>const msg=document.getElementById("msg");'
            'async function post(u,b){const r=await fetch(u,{method:"POST",headers:{"Content-Type":"application/json","X-Requested-With":"tfs"},body:JSON.stringify(b)});let j={};try{j=await r.json()}catch(e){}return {ok:r.ok,j}}'
            'async function add(w){const r=await post("/submit/api/access/add",{list:w,value:document.getElementById("v").value,note:document.getElementById("n").value});if(r.ok)location.reload();else msg.textContent=(r.j&&r.j.detail)||"Something went wrong";}'
            'document.getElementById("aa").onclick=()=>add("allow");document.getElementById("ad").onclick=()=>add("deny");'
            'document.querySelectorAll("button[data-act]").forEach(b=>b.onclick=async()=>{let r;if(b.dataset.act==="remove"){if(!confirm("Remove "+b.dataset.v+"?"))return;r=await post("/submit/api/access/remove",{list:b.dataset.from,value:b.dataset.v});}'
            'else{r=await post("/submit/api/access/add",{list:b.dataset.to,value:b.dataset.v});}if(r.ok)location.reload();else alert("Could not update");});</script>'
            + theme.FOOT + "</body></html>")
    return HTMLResponse(body, headers=NOCACHE)


def _need_header(request: Request) -> None:
    if request.headers.get("x-requested-with") != "tfs":
        raise HTTPException(status_code=403, detail="Forbidden")


@router.post("/submit/api/access/add")
async def api_add(request: Request):
    _need_header(request)
    b = await request.json()
    add_entry(str(b.get("list", "")), str(b.get("value", "")), str(b.get("note", "")))
    return JSONResponse({"ok": True}, headers=NOCACHE)


@router.post("/submit/api/access/remove")
async def api_remove(request: Request):
    _need_header(request)
    b = await request.json()
    remove_entry(str(b.get("list", "")), str(b.get("value", "")))
    return JSONResponse({"ok": True}, headers=NOCACHE)
