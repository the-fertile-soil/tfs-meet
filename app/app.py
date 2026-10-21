"""TFS meeting service web app.

Listens on 127.0.0.1:8100 only. nginx adds HTTPS in front of it.
  /submit/          upload form (nginx basic auth)
  /submit/upload    receives a recording (nginx basic auth)
  /review/<token>   private review page for one participant (the token is the secret)
"""
import hmac
import html
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response

from manage import retention_days, router as manage_router
from schedule import router as schedule_router
from availability import router as availability_router
from start import router as start_router
from access import router as access_router
from admin_api import router as admin_router

UPLOADS = Path("/opt/tfs-review/uploads")
UPLOADS.mkdir(parents=True, exist_ok=True)
MAX_BYTES = 3 * 1024 ** 3
MAX_RECIPIENTS = 10
ALLOWED = {".webm", ".mp4", ".mkv", ".mov", ".m4a", ".mp3", ".wav", ".ogg", ".opus"}
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.include_router(manage_router)
app.include_router(schedule_router)
app.include_router(availability_router)
app.include_router(start_router)
app.include_router(access_router)
app.include_router(admin_router)

import theme
import notify
import tfsfmt
STYLE = """
body{font-family:system-ui,sans-serif;max-width:720px;margin:32px auto;padding:0 16px;color:#1d2a1d;background:#f7f5ef;line-height:1.5}
h1{font-size:1.5rem}h2{font-size:1.15rem;margin-top:1.6em}
label{display:block;margin:16px 0 6px;font-weight:600}
input[type=text],textarea{width:100%;padding:10px;border:1px solid #9a9a8a;border-radius:6px;box-sizing:border-box;font:inherit}
button{margin-top:18px;padding:12px 20px;font-size:1rem;border:0;border-radius:6px;background:#2f6b3a;color:#fff;cursor:pointer}
button:disabled{background:#8aa58f;cursor:wait}
progress{width:100%;height:18px;margin-top:18px;display:none}
#msg{margin-top:16px;white-space:pre-wrap}.ok{color:#1f6b2d}.err{color:#a02020}
small,.note{color:#555;font-size:.9rem}
.box{background:#fff;border:1px solid #d8d4c4;border-radius:8px;padding:4px 18px 14px}
details{margin-top:1.4em}summary{cursor:pointer;font-weight:600}
.btn{display:inline-block;margin:8px 10px 0 0;padding:10px 18px;border-radius:6px;background:#2f6b3a;color:#fff;text-decoration:none;font-weight:600}
.btn.d{background:#a02020;border:0;cursor:pointer;font-size:1rem}
.btn.o{background:#fff;color:#2f6b3a;border:2px solid #2f6b3a;padding:8px 16px}
video{width:100%;max-height:70vh;border-radius:8px;background:#000;margin-top:8px}
pre{white-space:pre-wrap;font-family:inherit;background:#fff;border:1px solid #d8d4c4;border-radius:8px;padding:12px}
"""



def private_headers() -> dict:
    return {
        "X-Robots-Tag": "noindex, nofollow, noarchive",
        "Cache-Control": "no-store",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
    }


@app.get("/submit/")
async def submit_root():
    return RedirectResponse("/submit/meetings", status_code=302)


def parse_recipients(raw: str) -> list:
    seen, out = set(), []
    for item in re.split(r"[\s,;]+", raw or ""):
        item = item.strip().lower()
        if not item:
            continue
        if not EMAIL_RE.match(item) or len(item) > 200:
            raise HTTPException(status_code=400, detail="Not a valid email address: " + item[:60])
        if item not in seen:
            seen.add(item)
            out.append(item)
    if len(out) > MAX_RECIPIENTS:
        raise HTTPException(status_code=400, detail="At most 10 email addresses are allowed")
    return out


def inline(text: str) -> str:
    return re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", html.escape(text))


def render_markdown(md: str) -> str:
    out, in_list = [], False
    for line in md.splitlines():
        s = line.strip()
        if s.startswith("- ") or s.startswith("* "):
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append("<li>" + inline(s[2:]) + "</li>")
            continue
        if in_list:
            out.append("</ul>")
            in_list = False
        if s.startswith("## "):
            out.append("<h2>" + inline(s[3:]) + "</h2>")
        elif s:
            out.append("<p>" + inline(s) + "</p>")
    if in_list:
        out.append("</ul>")
    return "\n".join(out)


def find_meeting(token: str):
    if not re.fullmatch(r"[A-Za-z0-9_-]{20,100}", token):
        return None
    for p in UPLOADS.glob("*.json"):
        try:
            meta = json.loads(p.read_text())
        except Exception:
            continue
        for _email, tok in (meta.get("reviews") or {}).items():
            if hmac.compare_digest(tok, token):
                return meta
    return None


def token_email(meta: dict, token: str) -> str:
    for email, tok in (meta.get("reviews") or {}).items():
        if hmac.compare_digest(tok, token):
            return email
    return ""


def is_host(meta: dict, token: str) -> bool:
    h = (meta.get("host") or "").strip().lower()
    return bool(h) and token_email(meta, token).strip().lower() == h


def expired(meta: dict) -> bool:
    if meta.get("keep"):
        return False
    try:
        up = datetime.strptime(meta["uploaded_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except Exception:
        return True
    return datetime.now(timezone.utc) > up + timedelta(days=retention_days())


@app.get("/review/{token}", response_class=HTMLResponse)
async def review(token: str) -> HTMLResponse:
    meta = find_meeting(token)
    if meta is None or meta.get("status") != "done" or expired(meta):
        raise HTTPException(status_code=404, detail="This link is not valid or has expired.")
    summary = (UPLOADS / meta["summary"]).read_text() if meta.get("summary") else ""
    transcript = (UPLOADS / meta["transcript"]).read_text() if meta.get("transcript") else ""
    title = meta.get("title") or "TFS meeting"
    when = meta["uploaded_utc"][:10]
    page = (
        theme.top(title, eyebrow="Meeting summary", mode="public") +
        "<p class=\"note\">Example Org meeting recorded " + when + ". "
        "</p>"
        + media_block(token, meta, is_host(meta, token)) +
        "<div class=\"box\">" + render_markdown(summary) + "</div>"
        "<h2>People</h2><div class=\"box\"><ul>" + "".join("<li>" + html.escape(x) + "</li>" for x in notify.people_lines(meta)) + "</ul></div>"
        "<style>" + tfsfmt.CSS + "</style><h2 id=\"transcript\">Transcript</h2>" + tfsfmt.to_html(transcript) +
        "<p class=\"note\">This is your private link. " + ("This meeting has been saved, so the link keeps working." if meta.get("keep") else "It will stop working in " + str(retention_days()) + " days on " + expiry_text(meta) + " unless the meeting is saved.") + "</p>"
        "<p class=\"note\">Questions or comments, or did you spot a mistake? Contact <a href=\"mailto:meetings@example.org\">meetings@example.org</a>.</p>"
        + theme.FOOT + "</body></html>"
    )
    return HTMLResponse(page, headers=private_headers())


def expiry_text(meta: dict) -> str:
    up = datetime.strptime(meta["uploaded_utc"], "%Y-%m-%dT%H:%M:%SZ")
    d = up + timedelta(days=retention_days())
    return d.strftime("%B ") + str(d.day) + d.strftime(", %Y")


def media_block(token: str, meta: dict, host: bool = False) -> str:
    base = "/review/" + token
    out = "<div class=\"box\">"
    f = UPLOADS / meta.get("file", "")
    if meta.get("file") and f.is_file():
        out += ("<h2>The recording</h2><video controls preload=\"metadata\" playsinline src=\"" + base + "/video\"></video>"
                "<p><a class=\"btn\" href=\"" + base + "/download\">Download the video</a>")
    else:
        out += "<p>"
    out += "<a class=\"btn o\" href=\"" + base + "/pdf\">Save the notes as a PDF</a></p>"
    if host:
        out += ("<p class=\"note\">You started this meeting, so you can delete it. This permanently removes the recording, "
                "transcript and summary for everyone. It cannot be undone.</p>"
                "<p><button class=\"btn d\" id=\"del\" type=\"button\">Delete this meeting</button></p>"
                "<script>document.getElementById('del').onclick=function(){"
                "if(!confirm('Permanently delete this meeting, its recording, transcript and summary for everyone? This cannot be undone.'))return;"
                "fetch('" + base + "/delete',{method:'POST',headers:{'X-Requested-With':'tfs'}})"
                ".then(function(r){if(!r.ok)throw 0;document.body.innerHTML='<h1>Deleted</h1><p>This meeting and its recording have been permanently deleted.</p>';})"
                ".catch(function(){alert('Could not delete. It may still be processing. Please try again in a few minutes.');});};</script>")
    out += "</div>"
    return out


def slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-")[:60] or "meeting"


def valid_meeting(token: str) -> dict:
    meta = find_meeting(token)
    if meta is None or meta.get("status") != "done" or expired(meta):
        raise HTTPException(status_code=404, detail="This link is not valid or has expired.")
    return meta


def media_path(meta: dict) -> Path:
    f = UPLOADS / meta.get("file", "")
    if not meta.get("file") or not f.is_file():
        raise HTTPException(status_code=404, detail="The recording is no longer available.")
    return f


@app.get("/review/{token}/video")
async def review_video(token: str):
    meta = valid_meeting(token)
    f = media_path(meta)
    return FileResponse(f, media_type="video/mp4",
                        headers={"X-Robots-Tag": "noindex, nofollow", "Cache-Control": "private, no-cache",
                                 "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff"})


@app.get("/review/{token}/download")
async def review_download(token: str):
    meta = valid_meeting(token)
    f = media_path(meta)
    name = slug(meta.get("title") or "meeting") + "-" + meta["uploaded_utc"][:10] + f.suffix
    return FileResponse(f, media_type="video/mp4", filename=name, content_disposition_type="attachment",
                        headers={"X-Robots-Tag": "noindex, nofollow", "Cache-Control": "private, no-cache",
                                 "Referrer-Policy": "no-referrer"})


@app.get("/review/{token}/pdf")
async def review_pdf(token: str):
    from pdfout import make_pdf
    meta = valid_meeting(token)
    summary = (UPLOADS / meta["summary"]).read_text() if meta.get("summary") else ""
    transcript = (UPLOADS / meta["transcript"]).read_text() if meta.get("transcript") else ""
    title = meta.get("title") or "TFS meeting"
    data = make_pdf(title, meta["uploaded_utc"][:10], summary, transcript, notify.people_lines(meta))
    name = slug(title) + "-" + meta["uploaded_utc"][:10] + ".pdf"
    return Response(data, media_type="application/pdf",
                    headers={**private_headers(), "Content-Disposition": "attachment; filename=\"" + name + "\""})


@app.post("/review/{token}/delete")
async def review_delete(token: str, request: Request):
    if request.headers.get("x-requested-with") != "tfs":
        raise HTTPException(status_code=403, detail="Forbidden")
    meta = valid_meeting(token)
    if not is_host(meta, token):
        raise HTTPException(status_code=403, detail="Only the person who started the meeting can delete it.")
    from manage import delete_files
    n = delete_files(meta["file"])
    return JSONResponse({"ok": True, "files_deleted": n}, headers=private_headers())
