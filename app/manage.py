"""Moderator tools for TFS meetings: list, keep, download, delete, retention setting.

All routes live under /submit/, so nginx basic auth protects them.
POST routes also require an X-Requested-With header, which blocks cross-site forms.
"""
import html
import json
import re
import secrets
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
import theme
from starlette.concurrency import run_in_threadpool

import mailer

UPLOADS = Path("/opt/tfs-review/uploads")
CONFIG = Path("/opt/tfs-review/config.json")
DEFAULT_RETENTION_DAYS = 30
ID_RE = re.compile(r"^[0-9]{8}-[0-9]{6}_[A-Za-z0-9._-]+$")
BUSY = ("transcribing", "summarizing")
PUBLIC_BASE = "https://meet.example.org"
MAIL_FROM = "Example Org Meetings <meetings@example.org>"
EMAIL_RE = re.compile(r"^[^@\s,;<>\"']+@[^@\s,;<>\"']+\.[^@\s,;<>\"']+$")

router = APIRouter()

STYLE = """
body{font-family:system-ui,sans-serif;max-width:980px;margin:32px auto;padding:0 16px;color:#1d2a1d;background:#f7f5ef;line-height:1.5}
h1{font-size:1.5rem}h2{font-size:1.15rem;margin-top:1.6em}
table{width:100%;border-collapse:collapse;background:#fff;border:1px solid #d8d4c4}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid #e6e2d4;vertical-align:top;font-size:.95rem}
th{background:#efeadb}
button,a.btn{display:inline-block;margin:2px 4px 2px 0;padding:6px 10px;font-size:.9rem;border:0;border-radius:6px;background:#2f6b3a;color:#fff;cursor:pointer;text-decoration:none}
button.alt{background:#6b6b5a}button.danger{background:#a02020}
.pill{display:inline-block;padding:2px 8px;border-radius:10px;font-size:.8rem;background:#e6e2d4}
.pill.kept{background:#cfe8d3;color:#1f4d29}
.note{color:#555;font-size:.9rem}
.box{background:#fff;border:1px solid #d8d4c4;border-radius:8px;padding:4px 18px 14px}
pre{white-space:pre-wrap;font-family:inherit;background:#fff;border:1px solid #d8d4c4;border-radius:8px;padding:12px}
input[type=number]{width:80px;padding:6px}
"""

HEAD = ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<meta name=\"robots\" content=\"noindex,nofollow\"><title>__T__</title><style>"
        + STYLE + "</style></head><body>")

HEADERS = {"X-Robots-Tag": "noindex, nofollow, noarchive", "Cache-Control": "no-store",
           "Referrer-Policy": "no-referrer"}


def retention_days() -> int:
    try:
        d = int(json.loads(CONFIG.read_text()).get("retention_days", DEFAULT_RETENTION_DAYS))
    except Exception:
        d = DEFAULT_RETENTION_DAYS
    return min(max(d, 1), 365)


def load(file_id: str):
    if not ID_RE.match(file_id):
        raise HTTPException(status_code=404, detail="Not found")
    path = UPLOADS / (file_id + ".json")
    if not path.exists():
        raise HTTPException(status_code=404, detail="Not found")
    return path, json.loads(path.read_text())


def require_xhr(request: Request) -> None:
    if request.headers.get("x-requested-with") != "tfs":
        raise HTTPException(status_code=403, detail="Forbidden")


def uploaded_dt(meta: dict) -> datetime:
    return datetime.strptime(meta["uploaded_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def expiry_label(meta: dict) -> str:
    if meta.get("keep"):
        return "Saved (never deleted)"
    d = uploaded_dt(meta) + timedelta(days=retention_days())
    left = (d - datetime.now(timezone.utc)).days
    return d.strftime("%Y-%m-%d") + " (" + (str(left) + " days left" if left >= 0 else "due for deletion") + ")"


def delete_files(file_id: str) -> int:
    n = 0
    for p in UPLOADS.glob(file_id + "*"):
        if p.is_file():
            p.unlink()
            n += 1
    return n


def e(text) -> str:
    return html.escape(str(text))


@router.get("/submit/meetings", response_class=HTMLResponse)
async def meetings() -> HTMLResponse:
    rows = []
    for p in sorted(UPLOADS.glob("*.json"), reverse=True):
        try:
            m = json.loads(p.read_text())
        except Exception:
            continue
        fid = m["file"]
        kept = bool(m.get("keep"))
        title = m.get("title") or "(no name)"
        status = m.get("status", "")
        pill = "<span class=\"pill kept\">Saved</span> " if kept else ""
        btns = "<a class=\"btn\" href=\"/submit/view/" + e(fid) + "\">View</a>"
        btns += "<a class=\"btn alt\" href=\"/submit/download/" + e(fid) + "\">Download</a>"
        btns += "<a class=\"btn\" href=\"/submit/view/" + e(fid) + "#access\">Share</a>"
        if kept:
            btns += "<button class=\"alt\" data-act=\"unkeep\" data-id=\"" + e(fid) + "\">Stop keeping</button>"
        else:
            btns += "<button data-act=\"keep\" data-id=\"" + e(fid) + "\">Keep</button>"
        btns += "<button class=\"danger\" data-act=\"delete\" data-id=\"" + e(fid) + "\">Delete now</button>"
        rows.append(
            "<tr><td>" + pill + "<strong>" + e(title) + "</strong><br><span class=\"note\">" + e(fid) + "</span></td>"
            "<td>" + e(m.get("uploaded_utc", "")[:10]) + "</td>"
            "<td>" + e(status) + "</td>"
            "<td>" + str(len(m.get("recipients", []))) + "</td>"
            "<td>" + e(expiry_label(m)) + "</td><td>" + btns + "</td></tr>")
    table = ("<table><tr><th>Meeting</th><th>Uploaded</th><th>Status</th><th>Emailed</th>"
             "<th>Deleted on</th><th></th></tr>" + "".join(rows) + "</table>") if rows else "<p>No meetings yet.</p>"
    days = retention_days()
    page = (theme.top("Meetings and recordings", eyebrow="Moderator area",
                      lead="Recordings, transcripts and summaries from past meetings.", active="meetings", lift=True) +
            "<div class=\"card lift\"><h2>Your meetings</h2><div class=\"tablewrap\">" + table + "</div>"
            "<p><a class=\"btn\" href=\"https://meet.example.org/#latercard\">Schedule a meeting</a>"
            "<a class=\"btn o\" href=\"https://meet.example.org/\">Start a meeting now</a></p></div>"
            "<div class=\"card\"><h2>Automatic deletion</h2>"
            "<p>Meetings that are not saved are deleted after "
            "<input type=\"number\" id=\"days\" min=\"1\" max=\"365\" value=\"" + str(days) + "\"> days. "
            "<button id=\"savedays\">Save</button></p>"
            "<p class=\"note\">Saved meetings are never deleted and their review links never expire. "
            "Lowering this number deletes older unsaved meetings at the next nightly cleanup (about 3:30 AM UTC).</p></div>"
            "<script>"
            "async function post(path,body){const r=await fetch(path,{method:\"POST\",headers:{\"X-Requested-With\":\"tfs\","
            "\"Content-Type\":\"application/json\"},body:JSON.stringify(body||{})});"
            "if(!r.ok){alert(await r.text());return false;}return true;}"
            "document.querySelectorAll(\"button[data-act]\").forEach(b=>b.onclick=async()=>{"
            "const act=b.dataset.act,id=b.dataset.id;"
            "if(act===\"delete\"&&!confirm(\"Permanently delete this meeting, its recording, transcript and summary? This cannot be undone.\"))return;"
            "if(await post(\"/submit/api/\"+act+\"/\"+encodeURIComponent(id)))location.reload();});"
            "document.getElementById(\"savedays\").onclick=async()=>{"
            "const d=parseInt(document.getElementById(\"days\").value,10);"
            "if(!(d>=1&&d<=365)){alert(\"Enter a number from 1 to 365.\");return;}"
            "if(await post(\"/submit/api/settings\",{retention_days:d}))location.reload();};"
            "</script>" + theme.FOOT + "</body></html>")
    return HTMLResponse(page, headers=HEADERS)


VIEW_CSS = """
.page{padding-bottom:0}
.card h2{margin:0 0 4px;font-size:1.2rem;color:#1f4d29}
.sub{color:#5d6657;font-size:.92rem;margin:0 0 16px}
.card button,.card a.btn{margin:0;padding:9px 16px;font-size:.9rem}
.card button:disabled{opacity:.6;cursor:wait;transform:none}
.rec{display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap}
.rec h2{margin:0}
.people{list-style:none;margin:0;padding:0}
.person{display:grid;grid-template-columns:132px 1fr auto;align-items:center;gap:16px;padding:12px 0;border-top:1px solid #eee9d8}
.person:first-child{border-top:0}
.person button{width:132px}
.who{font-weight:600;overflow-wrap:anywhere}
.tag{font-size:.78rem;padding:3px 11px;border-radius:999px;background:#cfe8d3;color:#1f4d29;white-space:nowrap;font-weight:600}
.tag.wait{background:#f6e6b4;color:#6b4e00}
.addrow{display:grid;grid-template-columns:132px 1fr;gap:16px;align-items:center;margin-top:18px;padding-top:18px;border-top:2px dashed #e3dfcf}
.addrow button{width:132px}
.addrow input{padding:11px 14px;border:1px solid #cfc9b3;border-radius:10px;font-size:1rem;width:100%;box-sizing:border-box;background:#fbfaf5}
.addrow input:focus{outline:2px solid #2f6b3a;outline-offset:1px;background:#fff}
.fine{color:#6a7263;font-size:.85rem;margin:14px 0 0}
.summary h3{margin:18px 0 6px;font-size:1.05rem;color:#1f4d29}
.summary h3:first-child{margin-top:0}
.summary p{margin:6px 0;line-height:1.6}
.summary ul{margin:6px 0 6px 0;padding-left:22px}
.summary li{margin:5px 0;line-height:1.55}
details.card summary{cursor:pointer;font-size:1.2rem;font-weight:700;color:#1f4d29;list-style:none}
details.card summary::-webkit-details-marker{display:none}
details.card summary::before{content:"\\25B8";display:inline-block;margin-right:10px;transition:transform .15s}
details.card[open] summary::before{transform:rotate(90deg)}
.tx{max-height:480px;overflow:auto;background:#fbfaf5;border:1px solid #e3dfcf;border-radius:10px;padding:14px 16px;margin:14px 0 0;font-size:.92rem;line-height:1.6;white-space:pre-wrap}
@media (max-width:560px){.person{grid-template-columns:112px 1fr;gap:6px 12px}.person button{width:112px;padding:9px 8px}.person .tag{grid-column:2;justify-self:start}.addrow{grid-template-columns:1fr}.addrow button{width:100%}.hero h1{font-size:1.5rem}}
"""


def render_md(text: str) -> str:
    out, in_list, skipped = [], False, False
    for line in text.splitlines():
        t = line.rstrip()
        if t.startswith("- ") or t.startswith("* "):
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append("<li>" + e(t[2:]) + "</li>")
            continue
        if in_list:
            out.append("</ul>")
            in_list = False
        if t.startswith("## ") or t.startswith("# "):
            head = t.lstrip("#").strip()
            if head.lower() == "summary" and not skipped:
                skipped = True
                continue
            out.append("<h3>" + e(head) + "</h3>")
        elif t.strip():
            out.append("<p>" + e(t) + "</p>")
    if in_list:
        out.append("</ul>")
    return "".join(out)


@router.get("/submit/view/{file_id}", response_class=HTMLResponse)
async def view(file_id: str) -> HTMLResponse:
    _path, m = load(file_id)
    sfile, tfile = m.get("summary"), m.get("transcript")
    summary = (UPLOADS / sfile).read_text() if sfile and (UPLOADS / sfile).exists() else ""
    transcript = (UPLOADS / tfile).read_text() if tfile and (UPLOADS / tfile).exists() else ""
    title = m.get("title") or "(no name)"
    status = m.get("status", "")
    mins = int(round(float(m.get("audio_seconds", 0)) / 60)) if m.get("audio_seconds") else 0
    mb = int(round(m.get("bytes", 0) / 1048576)) if m.get("bytes") else 0

    chips = ("<span class=\"chip\" data-utc=\"" + e(m.get("uploaded_utc", "")) + "\">" + e(m.get("uploaded_utc", "")[:10]) + "</span>")
    if mins:
        chips += "<span class=\"chip\">" + str(mins) + " min</span>"
    chips += "<span class=\"chip\">" + e(status.capitalize()) + "</span>"
    chips += "<span class=\"chip\">" + e(expiry_label(m)) + "</span>"

    people = ""
    emailed = m.get("emailed_to") or []
    for addr in m.get("recipients", []):
        tag = "<span class=\"tag\">Emailed</span>" if addr in emailed else "<span class=\"tag wait\">Not yet emailed</span>"
        people += ("<li class=\"person\"><button class=\"alt\" data-email=\"" + e(addr) + "\">Resend link</button>"
                   "<span class=\"who\">" + e(addr) + "</span>" + tag + "</li>")
    if status == "done":
        adder = ("<div class=\"addrow\"><button id=\"addperson\">Add and send</button>"
                 "<input type=\"email\" id=\"newemail\" placeholder=\"Add someone: name@example.com\" autocomplete=\"off\"></div>")
    else:
        adder = "<p class=\"fine\">Sharing is available once the meeting has finished processing.</p>"

    script = ("<script>"
              "async function share(email,btn){if(btn){btn.disabled=true;btn.dataset.t=btn.textContent;btn.textContent=\"Sending...\";}"
              "const r=await fetch(\"/submit/api/share/" + e(file_id) + "\",{method:\"POST\",headers:{\"X-Requested-With\":\"tfs\","
              "\"Content-Type\":\"application/json\"},body:JSON.stringify({email:email})});const t=await r.text();"
              "if(!r.ok){alert(t);if(btn){btn.disabled=false;btn.textContent=btn.dataset.t;}return;}"
              "alert(\"Email sent to \"+email+\".\");location.reload();}"
              "document.querySelectorAll(\"button[data-email]\").forEach(b=>b.onclick=()=>share(b.dataset.email,b));"
              "const a=document.getElementById(\"addperson\");"
              "if(a){const go=()=>{const v=document.getElementById(\"newemail\").value.trim();"
              "if(!v){alert(\"Type an email address first.\");return;}share(v,a);};a.onclick=go;"
              "document.getElementById(\"newemail\").addEventListener(\"keydown\",ev=>{if(ev.key===\"Enter\")go();});}"
              "document.querySelectorAll(\"[data-utc]\").forEach(el=>{const d=new Date(el.dataset.utc);"
              "if(!isNaN(d))el.textContent=d.toLocaleString([], {dateStyle:\"medium\",timeStyle:\"short\"});});"
              "</script>")

    body = (
        "<div class=\"card lift rec\"><div><h2>Recording</h2><p class=\"sub\" style=\"margin:4px 0 0\">"
        + (str(mb) + " MB video file" if mb else "Meeting recording") + "</p></div>"
        "<a class=\"btn\" href=\"/submit/download/" + e(file_id) + "\">Download recording</a></div>"
        "<div class=\"card\"><h2 id=\"access\">Who has access</h2>"
        "<p class=\"sub\">Each person has their own private link. Resending uses their existing link.</p>"
        "<ul class=\"people\">" + (people or "<li class=\"fine\">Nobody yet.</li>") + "</ul>" + adder +
        "<p class=\"fine\">Adding someone does not change anyone else's link.</p></div>"
        "<div class=\"card summary\"><h2>Summary</h2><p class=\"sub\">Written by AI from an automatic transcript. It may contain mistakes.</p>"
        + (render_md(summary) or "<p>Not ready yet.</p>") + "</div>"
        + ("<details class=\"card\"><summary>Transcript</summary><div class=\"tx\">" + e(transcript) + "</div></details>" if transcript else "")
        + script + theme.FOOT + "</body></html>")
    page = theme.top(title, eyebrow="Meeting recording", back=("/submit/meetings", "All meetings"), chips=chips,
                     extra_css=VIEW_CSS, active="meetings", lift=True) + body
    return HTMLResponse(page, headers=HEADERS)


@router.get("/submit/download/{file_id}")
async def download(file_id: str):
    _path, m = load(file_id)
    media = UPLOADS / m["file"]
    if not media.exists():
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(media, filename=m["file"], headers={"Cache-Control": "no-store"})


@router.post("/submit/api/keep/{file_id}")
async def keep(file_id: str, request: Request):
    require_xhr(request)
    path, m = load(file_id)
    m["keep"] = True
    m["kept_utc"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    path.write_text(json.dumps(m, indent=2))
    return {"ok": True}


@router.post("/submit/api/unkeep/{file_id}")
async def unkeep(file_id: str, request: Request):
    require_xhr(request)
    path, m = load(file_id)
    m["keep"] = False
    m.pop("kept_utc", None)
    path.write_text(json.dumps(m, indent=2))
    return {"ok": True}


@router.post("/submit/api/delete/{file_id}")
async def delete(file_id: str, request: Request):
    require_xhr(request)
    _path, m = load(file_id)
    if m.get("status") in BUSY:
        raise HTTPException(status_code=409, detail="This meeting is being processed. Try again in a few minutes.")
    return {"ok": True, "files_deleted": delete_files(file_id)}


def build_review_email(m: dict, token: str) -> EmailMessage:
    title = m.get("title") or "your TFS meeting"
    if m.get("keep"):
        expiry = "This meeting is saved, so the link does not expire."
    else:
        d = uploaded_dt(m) + timedelta(days=retention_days())
        expiry = "It stops working on " + d.strftime("%B %d, %Y").replace(" 0", " ") + "."
    body = (
        "Hello,\n\n"
        "The recording of " + title + " has been transcribed and summarized.\n\n"
        "Review it here:\n" + PUBLIC_BASE + "/review/" + token + "\n\n"
        "This link was made for you. Please do not share it. " + expiry + "\n\n"
        "The summary is written by AI from an automatic transcript and may contain mistakes. "
        "Please check anything important against your own notes.\n\n"
        "Example Org\n"
    )
    msg = EmailMessage()
    msg["From"] = MAIL_FROM
    msg["Subject"] = "Review: " + title
    msg["Date"] = formatdate(localtime=False)
    msg["Message-ID"] = make_msgid(domain="example.org")
    msg["Auto-Submitted"] = "auto-generated"
    msg.set_content(body)
    return msg


@router.post("/submit/api/share/{file_id}")
async def share(file_id: str, request: Request):
    require_xhr(request)
    path, m = load(file_id)
    if m.get("status") != "done":
        raise HTTPException(status_code=409, detail="This meeting is still being processed. Try again when it is done.")
    body = await request.json()
    addr = str(body.get("email", "")).strip()
    if len(addr) > 254 or not EMAIL_RE.match(addr):
        raise HTTPException(status_code=400, detail="That does not look like a valid email address.")
    reviews = m.setdefault("reviews", {})
    existing = {k.lower(): k for k in reviews}
    key = existing.get(addr.lower())
    action = "resent" if key else "added"
    if not key:
        key = addr
        reviews[key] = secrets.token_urlsafe(32)
        recips = m.setdefault("recipients", [])
        if key not in recips:
            recips.append(key)
        path.write_text(json.dumps(m, indent=2))
    msg = build_review_email(m, reviews[key])
    msg["To"] = key
    try:
        await run_in_threadpool(mailer.send_message, msg)
    except Exception as exc:
        raise HTTPException(status_code=502, detail="The link was saved but the email could not be sent: " + str(exc)[:150])
    sent = m.setdefault("emailed_to", [])
    if key not in sent:
        sent.append(key)
    path.write_text(json.dumps(m, indent=2))
    return {"ok": True, "action": action}


@router.post("/submit/api/settings")
async def settings(request: Request):
    require_xhr(request)
    body = await request.json()
    try:
        d = int(body.get("retention_days"))
    except Exception:
        raise HTTPException(status_code=400, detail="retention_days must be a number")
    if not 1 <= d <= 365:
        raise HTTPException(status_code=400, detail="retention_days must be from 1 to 365")
    CONFIG.write_text(json.dumps({"retention_days": d}, indent=2))
    return {"ok": True, "retention_days": d}
