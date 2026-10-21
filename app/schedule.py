"""Scheduling for TFS meetings: reserve up to 90 minutes, invite attendees, link the recording to them.

Routes live under /submit/ so nginx basic auth protects them.
POST routes also require an X-Requested-With header.
"""
import html
import json
import re
import secrets
import shutil
import urllib.parse
import smtplib
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path
from zoneinfo import ZoneInfo

import mailer
from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

SCHEDULES = Path("/opt/tfs-review/schedules")
PUBLIC_BASE = "https://meet.example.org"
MAIL_FROM = "Example Org Meetings <meetings@example.org>"
REPLY_TO = "meetings-admin@example.org"
DEFAULT_HOST = "admin@example.org"
HOST_NAMES = {}
LENGTH = timedelta(minutes=90)
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
ROOM_RE = re.compile(r"^tfs-[a-z0-9]{6}$")
ZONES = {
    "Asia/Dhaka": "Dhaka",
    "Asia/Bangkok": "Bangkok",
    "Asia/Singapore": "Singapore",
    "Asia/Tokyo": "Tokyo",
    "Australia/Brisbane": "Brisbane",
    "Pacific/Noumea": "New Caledonia",
    "Pacific/Fiji": "Fiji",
    "Pacific/Auckland": "Auckland",
    "Pacific/Honolulu": "Hawaii",
    "Pacific/Gambier": "Gambier Islands",
    "America/Anchorage": "Alaska",
    "America/Los_Angeles": "Pacific",
    "America/Denver": "Mountain",
    "America/Chicago": "Central",
    "America/New_York": "Eastern",
    "America/Halifax": "Atlantic",
    "Atlantic/South_Georgia": "South Georgia",
    "Atlantic/Cape_Verde": "Cape Verde",
    "Atlantic/Reykjavik": "Iceland",
    "Europe/London": "London",
    "Europe/Paris": "Central Europe",
    "Europe/Moscow": "Moscow",
    "Asia/Dubai": "Dubai",
    "Asia/Karachi": "Pakistan",
}

router = APIRouter()
HEADERS = {"X-Robots-Tag": "noindex, nofollow, noarchive", "Cache-Control": "no-store",
           "Referrer-Policy": "no-referrer"}

import theme
STYLE = """
body{font-family:system-ui,sans-serif;max-width:860px;margin:32px auto;padding:0 16px;color:#1d2a1d;background:#f7f5ef;line-height:1.5}
h1{font-size:1.5rem}h2{font-size:1.15rem;margin-top:1.6em}
label{display:block;margin-top:12px;font-weight:600}
input,select,textarea{width:100%;padding:8px;font-size:1rem;box-sizing:border-box;border:1px solid #bbb;border-radius:6px}
textarea{min-height:90px}
button{margin-top:14px;padding:8px 14px;font-size:1rem;border:0;border-radius:6px;background:#2f6b3a;color:#fff;cursor:pointer}
button.danger{background:#a02020;margin:0;padding:4px 10px;font-size:.85rem}
table{width:100%;border-collapse:collapse;background:#fff;border:1px solid #d8d4c4}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid #e6e2d4;font-size:.95rem;vertical-align:top}
th{background:#efeadb}
.note{color:#555;font-size:.9rem}
#msg{margin-top:14px;font-weight:600}
"""


def concurrency_block(start, exclude_room=""):
    """Return an existing meeting that blocks this hour because MAX_VIDEO meetings would already be running."""
    from availability import MAX_VIDEO
    others = []
    for m in load_all():
        if m.get("cancelled") or m.get("room") == exclude_room:
            continue
        s2 = parse_utc(m["start_utc"])
        if start < s2 + LENGTH and s2 < start + LENGTH:
            others.append((s2, m))
    points = [start] + [s2 for s2, _ in others if s2 > start]
    for p in points:
        cover = [m for s2, m in others if s2 <= p < s2 + LENGTH]
        if len(cover) >= MAX_VIDEO:
            return cover[0]
    return None


def esc(x) -> str:
    return html.escape(str(x), quote=True)


def load_all():
    SCHEDULES.mkdir(parents=True, exist_ok=True)
    out = []
    for p in sorted(SCHEDULES.glob("tfs-*.json")):
        try:
            out.append(json.loads(p.read_text()))
        except Exception:
            continue
    return out


def parse_utc(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def fmt_local(start_utc: str, tz: str) -> str:
    local = parse_utc(start_utc).astimezone(ZoneInfo(tz))
    label = ZONES.get(tz, tz)
    return local.strftime("%A, %B %d, %Y at %I:%M %p").replace(" 0", " ") + " " + label + " time"


def ics_for(meta: dict, method: str = "REQUEST") -> str:
    start = parse_utc(meta["start_utc"])
    end = start + LENGTH
    f = "%Y%m%dT%H%M%SZ"
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Example Org//Meetings//EN",
        "METHOD:" + method, "BEGIN:VEVENT",
        "UID:" + meta["room"] + "@meet.example.org",
        "DTSTAMP:" + datetime.now(timezone.utc).strftime(f),
        "DTSTART:" + start.strftime(f), "DTEND:" + end.strftime(f),
        "SUMMARY:" + meta["title"].replace(",", " ").replace(";", " "),
        "DESCRIPTION:Join at " + PUBLIC_BASE + "/" + meta["room"],
        "URL:" + PUBLIC_BASE + "/" + meta["room"],
        "STATUS:" + ("CANCELLED" if method == "CANCEL" else "CONFIRMED"),
        "SEQUENCE:" + str(int(meta.get("sequence", 0)) + (1 if method == "CANCEL" else 0)),
        "END:VEVENT", "END:VCALENDAR",
    ]
    return "\r\n".join(lines) + "\r\n"


def send(to_addr: str, subject: str, body: str, ics: str = "", method: str = "REQUEST", host: str = "", html: str = "") -> None:
    msg = EmailMessage()
    trusted = host.lower() in HOST_NAMES or host.lower().endswith("@example.org")
    if host and trusted:
        msg["From"] = HOST_NAMES.get(host.lower(), host.split("@")[0]) + " <" + host + ">"
        msg["Reply-To"] = host
    elif host:
        msg["From"] = "Example Org Meetings <meetings@example.org>"
        msg["Reply-To"] = host
    else:
        msg["From"] = MAIL_FROM
        msg["Reply-To"] = REPLY_TO
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=False)
    msg["Message-ID"] = make_msgid(domain="example.org")
    msg["Auto-Submitted"] = "auto-generated"
    msg.set_content(body)
    if html:
        msg.add_alternative(html, subtype="html")
    if ics:
        msg.add_attachment(ics.encode(), maintype="text", subtype="calendar",
                           filename="meeting.ics", params={"method": method})
    mailer.send_message(msg, from_addr=host if (host and trusted) else "meetings@example.org")


UPLOAD_BASE = Path("/opt/tfs-review/uploads")
FILES_ROOT = UPLOAD_BASE / "example" / "meetings"
MAX_FILES = 10
MAX_FILE_BYTES = 250 * 1024 * 1024
LIMIT_MSG = "This meeting has reached its file limit. Please contact meetings@example.org to add more."
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,40}$")


def human(n) -> str:
    n = float(n)
    for u in ("bytes", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return (str(int(n)) if u == "bytes" else "%.1f" % n) + " " + u
        n /= 1024


def file_url(f: dict) -> str:
    return PUBLIC_BASE + "/files/" + f["token"] + "/" + urllib.parse.quote(f["name"])


def _vals(meta: dict) -> dict:
    start = parse_utc(meta["start_utc"])
    local = start.astimezone(ZoneInfo(meta["tz"]))
    end = start + LENGTH
    zone = ZONES.get(meta["tz"], meta["tz"])
    clock = local.strftime("%I:%M %p").lstrip("0")
    return {"{{ROOM}}": meta["room"], "{{TITLE}}": esc(meta["title"]),
            "{{TITLEURL}}": urllib.parse.quote(meta["title"]),
            "{{DATE}}": local.strftime("%A, %B ") + str(local.day) + local.strftime(", %Y"),
            "{{TIME}}": clock + " " + zone + " time",
            "{{SHORT}}": local.strftime("%A, %B ") + str(local.day) + " at " + clock + " " + zone,
            "{{SHORTRICH}}": "<strong>" + local.strftime("%A, %B ") + str(local.day) + "</strong> at <em>" + clock + " " + zone + "</em>",
            "{{MON}}": local.strftime("%b").upper(), "{{DAYNUM}}": str(local.day),
            "{{GSTART}}": start.strftime("%Y%m%dT%H%M%SZ"), "{{GEND}}": end.strftime("%Y%m%dT%H%M%SZ")}


def files_html(files: list) -> str:
    if not files:
        return ""
    rows = "".join(
        '<tr><td style="padding:10px 14px;border-top:1px solid #e6d9c8;font-size:15px;line-height:1.4;">'
        '<a href="' + esc(file_url(f)) + '" style="color:#4a5e3d;font-weight:700;">' + esc(f["name"]) + '</a> '
        '<span style="color:#6b5a4b;font-size:13px;">(' + human(f.get("size", 0)) + ')</span></td></tr>' for f in files)
    return ('<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
            'style="margin:18px 0 8px 0;background:#fffdf9;border:1px solid #e6d9c8;border-radius:12px;">'
            '<tr><td style="padding:14px 14px 10px 14px;font-size:14px;line-height:1.5;color:#2e2118;">'
            '<strong style="font-size:17px;">Files for this meeting</strong><br><span style="color:#6b5a4b;">The files linked below are attached to the meeting. Please click on a name to download the file.</span></td></tr>'
            + rows + '</table>')


def files_text(files: list) -> str:
    if not files:
        return ""
    return "Files for this meeting\nThe files linked below are attached to the meeting. Please click on a name to download the file.\n" + "".join(" - " + f["name"] + ": " + file_url(f) + "\n" for f in files) + "\n"


def manage_url(meta: dict) -> str:
    return PUBLIC_BASE + "/manage/" + meta["manage_token"]


def ensure_manage(path: Path, meta: dict) -> str:
    if not meta.get("manage_token"):
        meta["manage_token"] = secrets.token_urlsafe(18)
        path.write_text(json.dumps(meta, indent=2))
    return meta["manage_token"]


def manage_block(meta: dict) -> str:
    if not meta.get("manage_token"):
        return ""
    return ('<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
            'style="margin:14px 0 14px 0;background:#eef3e9;border:1px solid #b9c9ad;border-radius:12px;">'
            '<tr><td style="padding:16px 18px;font-size:14px;line-height:1.6;color:#2e2118;">'
            '<strong>You are the host. Manage this meeting online.</strong><br>'
            'See the meeting, move the time, add files, or cancel it:<br>'
            '<a href="' + esc(manage_url(meta)) + '" style="color:#4a5e3d;font-weight:700;">Open the host page</a><br>'
            '<span style="color:#6b5a4b;font-size:13px;">This link is only for you. Anyone who has it can change the meeting, so please do not forward it.</span>'
            '</td></tr></table>')


def manage_text(meta: dict) -> str:
    if not meta.get("manage_token"):
        return ""
    return ("You are the host. Manage this meeting online (see it, move the time, add files, or cancel it). "
            "This link is only for you, so please do not forward it:\n" + manage_url(meta) + "\n\n")


def invite_html(meta: dict, for_host: bool = False, banner: str = "") -> str:
    try:
        tpl = Path("/opt/tfs-review/invite_tpl.html").read_text()
    except Exception:
        return ""
    vals = _vals(meta)
    vals["{{FILES}}"] = files_html(meta.get("files", []))
    vals["{{MANAGE}}"] = manage_block(meta) if for_host else ""
    if banner:
        tpl = tpl.replace("<!-- when -->", '<p style="margin:0 0 16px 0;padding:12px 14px;background:#fff1d6;border-left:4px solid #c0562b;font-size:15px;line-height:1.5;color:#2e2118;font-weight:700;">' + esc(banner) + "</p>\n<!-- when -->", 1)
    for k, v in vals.items():
        tpl = tpl.replace(k, v)
    return tpl


def notice_html(meta: dict, new: list, extra: str = "", for_host: bool = False) -> str:
    try:
        tpl = Path("/opt/tfs-review/invite_tpl.html").read_text()
    except Exception:
        return ""
    head = tpl[:tpl.index("You're invited!</div>")]
    head = head[:head.rindex("<div")]
    tail = tpl[tpl.index("<!-- notice -->"):]
    xtra = ('<p style="margin:0 0 18px 0;padding:12px 14px;background:#f4eadc;border-left:4px solid #c0562b;'
            'font-size:14px;line-height:1.6;color:#2e2118;">' + esc(extra).replace(esc(_vals(meta)["{{SHORT}}"]), _vals(meta)["{{SHORTRICH}}"]) + '</p>') if extra else ""
    mid = (
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin:0 0 16px 0;"><tr><td bgcolor="#c0562b" style="background:#c0562b;border-radius:8px;padding:10px 18px;font-size:17px;line-height:1.2;letter-spacing:2px;text-transform:uppercase;color:#ffffff;font-weight:800;">New material to review</td></tr></table>'
        '<h1 style="margin:8px 0 6px 0;font-family:Georgia,\'Times New Roman\',serif;font-size:30px;line-height:1.15;color:#2e2118;font-weight:700;">{{TITLE}}</h1>'
        '<p style="margin:0 0 18px 0;font-size:16px;line-height:1.55;color:#6b5a4b;">Content has been added for review before the meeting on {{SHORTRICH}}.</p>'
        + xtra + files_html(new) +
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin:24px 0 10px 0;width:100%;"><tr>'
        '<td align="center" bgcolor="#5f7650" style="background:#5f7650;border-radius:999px;">'
        '<a href="https://meet.example.org/{{ROOM}}" style="display:block;padding:14px 24px;font-size:17px;font-weight:700;color:#ffffff;text-decoration:none;border-radius:999px;">Join link for the meeting</a>'
        '</td></tr></table>'
        '<p style="margin:0 0 22px 0;font-size:14px;line-height:1.6;color:#2e2118;text-align:center;">'
        '<a href="https://calendar.google.com/calendar/render?action=TEMPLATE&amp;text={{TITLEURL}}&amp;dates={{GSTART}}%2F{{GEND}}&amp;details=Join%20at%20https%3A%2F%2Fmeet.example.org%2F{{ROOM}}&amp;location=https%3A%2F%2Fmeet.example.org%2F{{ROOM}}" style="color:#c0562b;font-weight:700;">Add to Google Calendar</a></p>'
    )
    html_ = (head + mid + (manage_block(meta) if for_host else "") + tail).replace("<title>Invitation:", "<title>Update:")
    for k, v in _vals(meta).items():
        html_ = html_.replace(k, v)
    return html_


def notice_text(meta: dict, new: list, extra: str = "", for_host: bool = False) -> str:
    v = _vals(meta)
    return ("Hello,\n\nContent has been added for review before the meeting " + meta["title"] + " on "
            + fmt_local(meta["start_utc"], meta["tz"]) + ".\n\n" + files_text(new)
            + ("Note: " + extra + "\n\n" if extra else "")
            + "Join: " + PUBLIC_BASE + "/" + meta["room"] + "\n"
            + "Add to Google Calendar: https://calendar.google.com/calendar/render?action=TEMPLATE&text="
            + v["{{TITLEURL}}"] + "&dates=" + v["{{GSTART}}"] + "/" + v["{{GEND}}"] + "\n\n"
            + (manage_text(meta) if for_host else "") + "Questions: " + REPLY_TO + "\n\nExample Org\n")


def get_meta(room: str):
    if not ROOM_RE.match(room):
        raise HTTPException(status_code=404, detail="Not found")
    path = SCHEDULES / (room + ".json")
    if not path.exists():
        raise HTTPException(status_code=404, detail="Not found")
    meta = json.loads(path.read_text())
    if meta.get("cancelled"):
        raise HTTPException(status_code=404, detail="Not found")
    return path, meta


def store_file(path: Path, meta: dict, src: Path, name: str) -> dict:
    files = meta.setdefault("files", [])
    if len(files) >= MAX_FILES:
        raise HTTPException(status_code=409, detail=LIMIT_MSG)
    safe = re.sub(r"[^A-Za-z0-9._ -]+", "_", Path(name).name).strip(" .")[:150] or "file"
    local = parse_utc(meta["start_utc"]).astimezone(ZoneInfo(meta["tz"]))
    folder = FILES_ROOT / local.strftime("%y") / local.strftime("%m") / local.strftime("%d")
    folder.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(16)
    dest = folder / (meta["room"] + "_" + token[:6] + "_" + safe)
    shutil.move(str(src), str(dest))
    rec = {"token": token, "name": safe, "path": str(dest.relative_to(UPLOAD_BASE)),
           "size": dest.stat().st_size, "added_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "notified": False}
    files.append(rec)
    path.write_text(json.dumps(meta, indent=2))
    return rec


def do_notice(path: Path, meta: dict, mode: str, extra: str = "") -> dict:
    new = [f for f in meta.get("files", []) if not f.get("notified")]
    if not new:
        raise HTTPException(status_code=400, detail="There are no new files to announce")
    host = meta.get("host") or DEFAULT_HOST
    subject = "New material added: " + meta["title"]
    if mode == "preview":
        send(host, "[Preview, not yet sent to participants] " + subject, notice_text(meta, new, extra, True),
             host=host, html=notice_html(meta, new, extra, True))
        return {"ok": True, "to": host}
    if mode != "all":
        raise HTTPException(status_code=400, detail="Unknown mode")
    sent, errors = 0, []
    for a in meta.get("attendees", []):
        is_host = a.lower() == host.lower()
        try:
            send(a, subject, notice_text(meta, new, extra, is_host), host=host, html=notice_html(meta, new, extra, is_host))
            sent += 1
        except Exception as exc:
            errors.append(a + ": " + str(exc)[:80])
    if sent:
        for f in new:
            f["notified"] = True
        path.write_text(json.dumps(meta, indent=2))
    return {"ok": True, "sent": sent, "errors": errors}


def files_cell(m: dict) -> str:
    files = m.get("files", [])
    out = ["<td>"]
    for f in files:
        out.append('<div><a href="' + esc(file_url(f)) + '">' + esc(f["name"]) + "</a></div>")
    out.append('<div class="fm" data-room="' + esc(m["room"]) + '">')
    if len(files) >= MAX_FILES:
        out.append('<p class="note" style="color:#a02020">The file limit for this meeting has been reached. '
                   'Please contact <a href="mailto:meetings@example.org">meetings@example.org</a> to add more.</p>')
    else:
        out.append('<input type="file" multiple><button class="up">Add files</button>')
    pending = [f for f in files if not f.get("notified")]
    if pending and m.get("invited"):
        out.append('<p class="note">' + str(len(pending)) + ' new file(s) not yet emailed to participants.</p>'
                   '<button class="pv">Email me a preview</button> <button class="all">Send to everyone</button>')
    out.append('<div class="st note"></div></div></td>')
    return "".join(out)


EXTRA_JS = r"""
async function upload(room,file){const fd=new FormData();fd.append('file',file);
const r=await fetch('/submit/api/schedule/'+room+'/files',{method:'POST',headers:{'X-Requested-With':'tfs'},body:fd});
let j={};try{j=await r.json()}catch(e){}return {ok:r.ok,j};}
document.getElementById('go').onclick=async()=>{
msg.textContent='Working...';const fl=document.getElementById('files').files;
const res=await post('/submit/api/schedule',{title:document.getElementById('title').value,when:document.getElementById('when').value,tz:document.getElementById('tz').value,emails:document.getElementById('emails').value,host:document.getElementById('host').value,defer:fl.length>0});
if(!res.ok){msg.textContent=(res.j&&res.j.detail)||'Something went wrong';return;}
if(fl.length){for(let i=0;i<fl.length;i++){msg.textContent='Uploading file '+(i+1)+' of '+fl.length+'...';
const r=await upload(res.j.room,fl[i]);if(!r.ok){msg.textContent=((r.j&&r.j.detail)||'Upload failed')+' The meeting is reserved but invitations were not sent. Reload this page and use Add files on the meeting.';return;}}
const s=await post('/submit/api/schedule/'+res.j.room+'/send-invites',{});
if(!s.ok){msg.textContent='Files saved but the invitations failed: '+((s.j&&s.j.detail)||'error');return;}
res.j.invited=s.j.invited;}
msg.textContent='Reserved. Room link: '+res.j.url+' . Invitations sent: '+res.j.invited;setTimeout(()=>location.reload(),2500);};
document.querySelectorAll('.fm').forEach(box=>{const room=box.dataset.room,st=box.querySelector('.st');
const up=box.querySelector('.up');if(up)up.onclick=async()=>{const fl=box.querySelector('input[type=file]').files;
if(!fl.length){st.textContent='Choose a file first.';return;}
for(const f of fl){st.textContent='Uploading '+f.name+'...';const r=await upload(room,f);if(!r.ok){st.textContent=(r.j&&r.j.detail)||'Upload failed';return;}}
location.reload();};
const pv=box.querySelector('.pv');if(pv)pv.onclick=async()=>{st.textContent='Sending preview...';
const r=await post('/submit/api/schedule/'+room+'/files-notice',{mode:'preview'});
st.textContent=r.ok?'Preview sent to '+r.j.to+'. Check your inbox.':((r.j&&r.j.detail)||'Could not send');};
const al=box.querySelector('.all');if(al)al.onclick=async()=>{if(!confirm('Email everyone invited to this meeting?'))return;
st.textContent='Sending...';const r=await post('/submit/api/schedule/'+room+'/files-notice',{mode:'all'});
if(r.ok){st.textContent='Sent to '+r.j.sent+' people.';setTimeout(()=>location.reload(),1500);}else st.textContent=(r.j&&r.j.detail)||'Could not send';};});
"""


def _invite_body_with_files(meta: dict, for_host: bool = False) -> str:
    base = _invite_body_base(meta)
    ft = files_text(meta.get("files", [])) + (manage_text(meta) if for_host else "")
    return base.replace("Please note:", ft + "Please note:", 1) if ft else base


def _invite_body_base(meta: dict) -> str:
    return (
        "Hello,\n\n"
        "You are invited to: " + meta["title"] + "\n"
        "When: " + fmt_local(meta["start_utc"], meta["tz"]) + " (up to 90 minutes)\n"
        "Join: " + PUBLIC_BASE + "/" + meta["room"] + "\n"
        "Hosting? Start the meeting here (use the email address this invitation was sent to; opens 15 minutes early): "
        + PUBLIC_BASE + "/?room=" + meta["room"] + "\n\n"
        "Please note: this meeting is recorded, transcribed, and summarized by AI software on "
        "Example Org's own server. After the meeting, you will get an email with a private "
        "link to the summary. Recordings that are not saved are deleted automatically after 30 days. "
        "By joining, you agree to be recorded. If you do not want to be recorded, tell the host before "
        "the meeting starts.\n\n"
        "Questions: " + REPLY_TO + "\n\nExample Org\n"
    )


@router.get("/submit/schedule", response_class=HTMLResponse)
async def page() -> HTMLResponse:
    now = datetime.now(timezone.utc)
    rows = []
    for m in load_all():
        if parse_utc(m["start_utc"]) + LENGTH < now or m.get("cancelled"):
            continue
        rows.append(
            "<tr><td>" + esc(m["title"]) + "</td><td>" + esc(fmt_local(m["start_utc"], m["tz"])) + "</td>"
            "<td><a href=\"" + PUBLIC_BASE + "/" + esc(m["room"]) + "\">" + esc(m["room"]) + "</a></td>"
            "<td>" + esc(len(m.get("attendees", []))) + "</td>" + files_cell(m) +
            "<td><button class=\"danger\" data-room=\"" + esc(m["room"]) + "\">Cancel</button></td></tr>"
        )
    table = ("<table><tr><th>Meeting</th><th>When</th><th>Room</th><th>Invited</th><th>Files</th><th></th></tr>"
             + "".join(rows) + "</table>") if rows else "<p class=\"note\">No upcoming meetings.</p>"
    zones = "".join("<option value=\"" + k + "\"" + (" selected" if k == "America/Los_Angeles" else "") + ">" + v + "</option>" for k, v in ZONES.items())
    body = (
        theme.top("Schedule a meeting", eyebrow="Moderator area",
                  lead="Up to two video meetings can run at the same time, up to 8 people each, up to 90 minutes. Everyone you list gets an invitation with a calendar file, and, after the meeting is complete, a private summary.",
                  active="scheduled", lift=True) +
        "<div class=\"card lift\"><h2>New meeting</h2>"
        "<label>Title<input id=\"title\" maxlength=\"120\" placeholder=\"Board meeting\"></label>"
        "<label>Date and time<input id=\"when\" type=\"datetime-local\"></label>"
        "<label>Time zone<select id=\"tz\">" + zones + "</select></label>"
        "<label>Host email (sends the invitations and replies come here)<input id=\"host\" type=\"email\" value=\"admin@example.org\"></label>"
        "<label>Attendee emails (one per line or separated by commas, up to 10)"
        "<textarea id=\"emails\"></textarea></label>"
        "<label>Files to attach (optional). Each one is sent as a download link in the invitation.<input id=\"files\" type=\"file\" multiple></label>"
        "<button id=\"go\">Reserve the time and send invitations</button>"
        "<div id=\"msg\"></div></div>"
        "<div class=\"card\"><h2>Upcoming meetings</h2><div class=\"tablewrap\">" + table + "</div>"
        "<p><a class=\"btn o\" href=\"/submit/meetings\">Past meetings and recordings</a></p></div>" +
        "<script>"
        "const msg=document.getElementById('msg');"
        "async function post(url,data){const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json','X-Requested-With':'tfs'},body:JSON.stringify(data)});"
        "let j={};try{j=await r.json()}catch(e){}return {ok:r.ok,j};}"
        "document.getElementById('go').onclick=async()=>{msg.textContent='Working...';"
        "const res=await post('/submit/api/schedule',{title:document.getElementById('title').value,when:document.getElementById('when').value,tz:document.getElementById('tz').value,emails:document.getElementById('emails').value,host:document.getElementById('host').value});"
        "if(res.ok){msg.textContent='Reserved. Room link: '+res.j.url+' . Invitations sent: '+res.j.invited;setTimeout(()=>location.reload(),2500);}"
        "else{msg.textContent=(res.j&&res.j.detail)||'Something went wrong';}};"
        "document.querySelectorAll('button[data-room]').forEach(b=>b.onclick=async()=>{if(!confirm('Cancel this meeting and notify attendees?'))return;"
        "const res=await post('/submit/api/schedule/cancel/'+b.dataset.room,{});if(res.ok)location.reload();else alert('Could not cancel');});"
        "</script><script>" + EXTRA_JS + "</script>" + theme.FOOT + "</body></html>"
    )
    return HTMLResponse(body, headers=HEADERS)


def need_header(request: Request) -> None:
    if request.headers.get("x-requested-with") != "tfs":
        raise HTTPException(status_code=403, detail="Forbidden")


@router.post("/submit/api/schedule")
async def create(request: Request):
    need_header(request)
    data = await request.json()
    title = re.sub(r"\s+", " ", str(data.get("title", ""))).strip()[:120] or "TFS meeting"
    tz = str(data.get("tz", ""))
    if tz not in ZONES:
        raise HTTPException(status_code=400, detail="Pick a time zone")
    try:
        local = datetime.strptime(str(data.get("when", "")), "%Y-%m-%dT%H:%M").replace(tzinfo=ZoneInfo(tz))
    except ValueError:
        raise HTTPException(status_code=400, detail="Enter a valid date and time")
    start = local.astimezone(timezone.utc)
    now = datetime.now(timezone.utc)
    if start < now + timedelta(minutes=5):
        raise HTTPException(status_code=400, detail="Pick a time in the future")
    if start > now + timedelta(days=92):
        raise HTTPException(status_code=400, detail="Pick a date within the next 3 months")
    host = str(data.get("host", "") or DEFAULT_HOST).strip()
    if not EMAIL_RE.match(host):
        raise HTTPException(status_code=400, detail="Host email is not valid")
    raw = [host] + re.split(r"[\s,;]+", str(data.get("emails", "")))
    attendees, seen = [], set()
    for e in raw:
        e = e.strip()
        if not e:
            continue
        if not EMAIL_RE.match(e):
            raise HTTPException(status_code=400, detail="Not a valid email address: " + e[:80])
        if e.lower() not in seen:
            seen.add(e.lower())
            attendees.append(e)
    if len(attendees) > 10:
        raise HTTPException(status_code=400, detail="At most 10 attendees")
    m = concurrency_block(start)
    if m:
        raise HTTPException(status_code=409, detail="Two meetings are already booked for that hour, including: "
                            + m["title"] + " at " + fmt_local(m["start_utc"], m["tz"]))
    room = "tfs-" + "".join(secrets.choice("abcdefghjkmnpqrstuvwxyz23456789") for _ in range(6))
    meta = {"room": room, "title": title, "start_utc": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "tz": tz, "host": host, "files": [], "manage_token": secrets.token_urlsafe(18), "attendees": attendees, "created_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "invited": []}
    SCHEDULES.mkdir(parents=True, exist_ok=True)
    path = SCHEDULES / (room + ".json")
    path.write_text(json.dumps(meta, indent=2))
    errors = []
    for a in ([] if data.get("defer") else attendees):
        try:
            send(a, "Invitation: " + title, _invite_body_with_files(meta, a.lower() == host.lower()), ics_for(meta), host=host, html=invite_html(meta, a.lower() == host.lower()))
            meta["invited"].append(a)
        except Exception as exc:
            errors.append(a + ": " + str(exc)[:80])
    if errors:
        meta["invite_errors"] = errors
    path.write_text(json.dumps(meta, indent=2))
    return JSONResponse({"ok": True, "room": room, "url": PUBLIC_BASE + "/" + room,
                         "invited": len(meta["invited"]), "errors": errors}, headers=HEADERS)


def do_cancel(path: Path, meta: dict) -> None:
    if meta.get("cancelled"):
        return
    meta["cancelled"] = True
    for a in meta.get("attendees", []):
        try:
            send(a, "Cancelled: " + meta["title"],
                 "The meeting " + meta["title"] + " on " + fmt_local(meta["start_utc"], meta["tz"])
                 + " has been cancelled.\n\nExample Org\n", ics_for(meta, "CANCEL"), "CANCEL", host=meta.get("host", ""))
        except Exception:
            pass
    path.write_text(json.dumps(meta, indent=2))


@router.post("/submit/api/schedule/cancel/{room}")
async def cancel(room: str, request: Request):
    need_header(request)
    if not ROOM_RE.match(room):
        raise HTTPException(status_code=404, detail="Not found")
    path = SCHEDULES / (room + ".json")
    if not path.exists():
        raise HTTPException(status_code=404, detail="Not found")
    do_cancel(path, json.loads(path.read_text()))
    return JSONResponse({"ok": True}, headers=HEADERS)


@router.post("/submit/api/schedule/{room}/files")
async def upload_file(room: str, request: Request, file: UploadFile = File(...)):
    need_header(request)
    path, meta = get_meta(room)
    if len(meta.get("files", [])) >= MAX_FILES:
        raise HTTPException(status_code=409, detail=LIMIT_MSG)
    tmp = Path("/tmp") / ("tfs-up-" + secrets.token_hex(6))
    size = 0
    try:
        with open(tmp, "wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_FILE_BYTES:
                    raise HTTPException(status_code=413, detail="That file is too large (250 MB maximum)")
                out.write(chunk)
        if size == 0:
            raise HTTPException(status_code=400, detail="That file is empty")
        rec = store_file(path, meta, tmp, file.filename or "file")
    finally:
        if tmp.exists():
            tmp.unlink()
    return JSONResponse({"ok": True, "name": rec["name"], "count": len(meta["files"])}, headers=HEADERS)


@router.post("/submit/api/schedule/{room}/send-invites")
async def send_invites(room: str, request: Request):
    need_header(request)
    path, meta = get_meta(room)
    errors = []
    for a in meta.get("attendees", []):
        if a in meta.get("invited", []):
            continue
        try:
            is_host = a.lower() == (meta.get("host") or "").lower()
            send(a, "Invitation: " + meta["title"], _invite_body_with_files(meta, is_host), ics_for(meta),
                 host=meta.get("host", ""), html=invite_html(meta, is_host))
            meta.setdefault("invited", []).append(a)
        except Exception as exc:
            errors.append(a + ": " + str(exc)[:80])
    for f in meta.get("files", []):
        f["notified"] = True
    if errors:
        meta["invite_errors"] = errors
    path.write_text(json.dumps(meta, indent=2))
    return JSONResponse({"ok": True, "invited": len(meta.get("invited", [])), "errors": errors}, headers=HEADERS)


@router.post("/submit/api/schedule/{room}/files-notice")
async def files_notice(room: str, request: Request):
    need_header(request)
    data = await request.json()
    path, meta = get_meta(room)
    return JSONResponse(do_notice(path, meta, str(data.get("mode", "")), ""), headers=HEADERS)


@router.get("/files/{token}/{name}")
async def download(token: str, name: str):
    if not TOKEN_RE.match(token):
        raise HTTPException(status_code=404, detail="Not found")
    for m in load_all():
        if m.get("cancelled"):
            continue
        for f in m.get("files", []):
            if f.get("token") == token:
                p = UPLOAD_BASE / f["path"]
                if p.is_file():
                    return FileResponse(str(p), filename=f["name"], headers=HEADERS)
    raise HTTPException(status_code=404, detail="Not found")


def do_reschedule(path: Path, meta: dict, when: str, tz: str) -> dict:
    if tz not in ZONES:
        raise HTTPException(status_code=400, detail="Pick a time zone")
    try:
        local = datetime.strptime(str(when), "%Y-%m-%dT%H:%M").replace(tzinfo=ZoneInfo(tz))
    except ValueError:
        raise HTTPException(status_code=400, detail="Enter a valid date and time")
    start = local.astimezone(timezone.utc)
    now = datetime.now(timezone.utc)
    if start < now + timedelta(minutes=5):
        raise HTTPException(status_code=400, detail="Pick a time in the future")
    if start > now + timedelta(days=92):
        raise HTTPException(status_code=400, detail="Pick a date within the next 3 months")
    m = concurrency_block(start, meta["room"])
    if m:
        raise HTTPException(status_code=409, detail="Two meetings are already booked for that hour, including: "
                            + m["title"] + " at " + fmt_local(m["start_utc"], m["tz"]))
    old = fmt_local(meta["start_utc"], meta["tz"])
    meta["start_utc"] = start.strftime("%Y-%m-%dT%H:%M:%SZ")
    meta["tz"] = tz
    meta["sequence"] = int(meta.get("sequence", 0)) + 1
    path.write_text(json.dumps(meta, indent=2))
    new = fmt_local(meta["start_utc"], meta["tz"])
    host = meta.get("host") or DEFAULT_HOST
    sent, errors = 0, []
    for a in meta.get("attendees", []):
        is_host = a.lower() == host.lower()
        text = ("The time of this meeting has changed.\n\nMeeting: " + meta["title"] + "\nOld time: " + old
                + "\nNew time: " + new + "\nJoin: " + PUBLIC_BASE + "/" + meta["room"] + "\n\n"
                + files_text(meta.get("files", [])) + (manage_text(meta) if is_host else "")
                + "Questions: " + REPLY_TO + "\n\nExample Org\n")
        try:
            send(a, "Updated time: " + meta["title"], text, ics_for(meta), host=host,
                 html=invite_html(meta, is_host, "The time has changed. New time: " + new))
            sent += 1
        except Exception as exc:
            errors.append(a + ": " + str(exc)[:80])
    return {"ok": True, "when": new, "sent": sent, "errors": errors}


def find_by_token(token: str):
    if not TOKEN_RE.match(token):
        raise HTTPException(status_code=404, detail="Not found")
    for p in sorted(SCHEDULES.glob("tfs-*.json")):
        try:
            m = json.loads(p.read_text())
        except Exception:
            continue
        if m.get("manage_token") == token:
            return p, m
    raise HTTPException(status_code=404, detail="Not found")


PEOPLE_CSS = """
.ppl{border:1px solid #d6cfc2;border-radius:10px;overflow:hidden;background:#fff;max-width:640px}
.prow{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:9px 12px 9px 14px;border-top:1px solid #ebe5d9}
.prow:first-child{border-top:0}
.pem{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:14px;color:#2a2a26;overflow-wrap:anywhere}
.ptag{font-size:11px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:#4d6240;background:#e9efe4;border-radius:5px;padding:3px 9px;min-width:74px;text-align:center;box-sizing:border-box}
.ppl .rm{min-width:74px;box-sizing:border-box;background:transparent!important;color:#6b6456!important;border:1px solid #cdc6b8!important;border-radius:6px!important;padding:4px 10px!important;font-size:11px!important;font-weight:700!important;letter-spacing:.08em;text-transform:uppercase;line-height:1.4;box-shadow:none!important;cursor:pointer;transition:all .12s}
.ppl .rm:hover{color:#a02020!important;border-color:#a02020!important;background:#fdf2f2!important}
.addrow{display:flex;gap:8px;margin-top:12px;max-width:640px}
.addrow input{flex:1;min-width:0;margin:0!important;border-radius:8px!important;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:14px}
.addrow button{border-radius:8px!important;white-space:nowrap;margin:0!important}
@media(max-width:520px){.addrow{flex-direction:column}}
"""


MAX_ATTENDEES = 8
EMAIL_OK = __import__("re").compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


MANAGE_JS = r"""
const T='__T__',msg=document.getElementById('msg');
async function post(url,data){const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json','X-Requested-With':'tfs'},body:JSON.stringify(data)});
let j={};try{j=await r.json()}catch(e){}return {ok:r.ok,j};}
async function upload(file){const fd=new FormData();fd.append('file',file);
const r=await fetch('/manage/'+T+'/api/files',{method:'POST',headers:{'X-Requested-With':'tfs'},body:fd});
let j={};try{j=await r.json()}catch(e){}return {ok:r.ok,j};}
const up=document.getElementById('up');if(up)up.onclick=async()=>{const fl=document.getElementById('fl').files;
if(!fl.length){msg.textContent='Choose a file first.';return;}
for(const f of fl){msg.textContent='Uploading '+f.name+'...';const r=await upload(f);if(!r.ok){msg.textContent=(r.j&&r.j.detail)||'Upload failed';return;}}
location.reload();};
const pv=document.getElementById('pv');if(pv)pv.onclick=async()=>{msg.textContent='Sending preview...';
const r=await post('/manage/'+T+'/api/notice',{mode:'preview'});
msg.textContent=r.ok?'Preview sent to '+r.j.to+'. Check your inbox.':((r.j&&r.j.detail)||'Could not send');};
const al=document.getElementById('all');if(al)al.onclick=async()=>{if(!confirm('Email everyone invited to this meeting about the new files?'))return;
msg.textContent='Sending...';const r=await post('/manage/'+T+'/api/notice',{mode:'all'});
if(r.ok){msg.textContent='Sent to '+r.j.sent+' people.';setTimeout(()=>location.reload(),1500);}else msg.textContent=(r.j&&r.j.detail)||'Could not send';};
document.querySelectorAll('.rm').forEach(b=>b.onclick=async()=>{const e=b.dataset.e;if(!confirm('Remove '+e+' from this meeting? They will be emailed that they are no longer invited.'))return;
const r=await post('/manage/'+T+'/api/remove-invitee',{email:e});if(r.ok)location.reload();else msg.textContent=(r.j&&r.j.detail)||'Could not remove';});
const ad=document.getElementById('addbtn');if(ad)ad.onclick=async()=>{const v=document.getElementById('addem').value.trim();
if(!v){msg.textContent='Type an email address first.';return;}
msg.textContent='Adding and sending the invitation...';const r=await post('/manage/'+T+'/api/add-invitee',{email:v});
if(r.ok){msg.textContent='Invitation sent to '+r.j.email+'.';setTimeout(()=>location.reload(),1200);}else msg.textContent=(r.j&&r.j.detail)||'Could not add';};
document.getElementById('mv').onclick=async()=>{if(!confirm('Move the meeting and email everyone the new time?'))return;
msg.textContent='Working...';const r=await post('/manage/'+T+'/api/reschedule',{when:document.getElementById('mvwhen').value,tz:document.getElementById('mvtz').value});
if(r.ok){msg.textContent='Moved to '+r.j.when+'. Emails sent: '+r.j.sent;setTimeout(()=>location.reload(),2000);}else msg.textContent=(r.j&&r.j.detail)||'Could not move the meeting';};
document.getElementById('cx').onclick=async()=>{if(!confirm('Cancel this meeting and notify everyone?'))return;
const r=await post('/manage/'+T+'/api/cancel',{});if(r.ok)location.reload();else msg.textContent='Could not cancel';};
"""


def render_manage(meta: dict) -> str:
    head = theme.top(meta["title"], eyebrow="Host page", active="", lift=True,
                     lead="Your private page for this meeting. Please do not share this link.")
    if meta.get("cancelled"):
        return head + "<div class=\"card lift\"><h2>Cancelled</h2><p>This meeting has been cancelled.</p></div>" + theme.FOOT + "</body></html>"
    files = meta.get("files", [])
    host = meta.get("host") or DEFAULT_HOST
    local = parse_utc(meta["start_utc"]).astimezone(ZoneInfo(meta["tz"]))
    tzopts = "".join("<option value=\"" + k + "\"" + (" selected" if k == meta["tz"] else "") + ">" + v + "</option>" for k, v in ZONES.items())
    flist = "".join("<li><a href=\"" + esc(file_url(f)) + "\">" + esc(f["name"]) + "</a> (" + human(f.get("size", 0)) + ")</li>" for f in files)
    flist = "<ul>" + flist + "</ul>" if flist else "<p class=\"note\">No files yet.</p>"
    if len(files) >= MAX_FILES:
        adder = ("<p class=\"note\" style=\"color:#a02020\">The file limit for this meeting has been reached. Please contact "
                 "<a href=\"mailto:meetings@example.org\">meetings@example.org</a> to add more.</p>")
    else:
        adder = "<input id=\"fl\" type=\"file\" multiple><button id=\"up\">Add files</button>"
    pending = [f for f in files if not f.get("notified")]
    pend = ("<p class=\"note\">" + str(len(pending)) + " new file(s) have not been emailed to the people invited.</p>"
            "<button id=\"pv\">Email me a preview</button> <button id=\"all\">Send to everyone</button>") if pending and meta.get("invited") else ""
    people = "".join("<div class=\"prow\"><span class=\"pem\">" + esc(a) + "</span>" + ("<span class=\"ptag\">Host</span>" if a.lower() == host.lower() else "<button class=\"rm\" data-e=\"" + esc(a) + "\">Remove</button>") + "</div>" for a in meta.get("attendees", []))
    addform = ("<div class=\"addrow\"><input id=\"addem\" type=\"email\" placeholder=\"name@example.com\" autocomplete=\"off\"><button id=\"addbtn\">Add and invite</button></div>"
               if len(meta.get("attendees", [])) < MAX_ATTENDEES else "<p class=\"note\">This meeting is full (" + str(MAX_ATTENDEES) + " people).</p>")
    pstyle = PEOPLE_CSS
    body = (head + "<style>" + pstyle + "</style><div class=\"card lift\"><h2>The meeting</h2><div class=\"tablewrap\"><table>"
            "<tr><th>When</th><td>" + esc(fmt_local(meta["start_utc"], meta["tz"])) + " (up to 90 minutes)</td></tr>"
            "<tr><th>Join link</th><td><a href=\"" + PUBLIC_BASE + "/" + esc(meta["room"]) + "\">" + PUBLIC_BASE + "/" + esc(meta["room"]) + "</a></td></tr>"
            "<tr><th>Host</th><td>" + esc(host) + "</td></tr>"
            "<tr><th>Invited</th><td><div class=\"ppl\">" + people + "</div>" + addform + "</td></tr></table></div>"
            "</div>"
            "<div class=\"card\"><h2>Files</h2>" + flist + adder + pend + "</div>"
            "<div class=\"card\"><h2>Move the meeting</h2><label>New date and time<input id=\"mvwhen\" type=\"datetime-local\" value=\"" + local.strftime("%Y-%m-%dT%H:%M") + "\"></label>"
            "<label>Time zone<select id=\"mvtz\">" + tzopts + "</select></label>"
            "<button id=\"mv\">Move the meeting and email everyone</button></div>"
            "<div class=\"card\"><h2>Cancel the meeting</h2><p class=\"note\">Everyone invited is emailed that it is cancelled.</p>"
            "<button class=\"danger\" id=\"cx\">Cancel this meeting</button></div>"
            "<div id=\"msg\"></div><script>" + MANAGE_JS.replace("__T__", meta["manage_token"]) + "</script>" + theme.FOOT + "</body></html>")
    return body


@router.get("/manage/{token}", response_class=HTMLResponse)
async def manage_page(token: str):
    path, meta = find_by_token(token)
    return HTMLResponse(render_manage(meta), headers=HEADERS)


@router.post("/manage/{token}/api/files")
async def manage_upload(token: str, request: Request, file: UploadFile = File(...)):
    need_header(request)
    path, meta = find_by_token(token)
    if meta.get("cancelled"):
        raise HTTPException(status_code=404, detail="Not found")
    if len(meta.get("files", [])) >= MAX_FILES:
        raise HTTPException(status_code=409, detail=LIMIT_MSG)
    tmp = Path("/tmp") / ("tfs-up-" + secrets.token_hex(6))
    size = 0
    try:
        with open(tmp, "wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_FILE_BYTES:
                    raise HTTPException(status_code=413, detail="That file is too large (250 MB maximum)")
                out.write(chunk)
        if size == 0:
            raise HTTPException(status_code=400, detail="That file is empty")
        rec = store_file(path, meta, tmp, file.filename or "file")
    finally:
        if tmp.exists():
            tmp.unlink()
    return JSONResponse({"ok": True, "name": rec["name"], "count": len(meta["files"])}, headers=HEADERS)


@router.post("/manage/{token}/api/notice")
async def manage_notice(token: str, request: Request):
    need_header(request)
    data = await request.json()
    path, meta = find_by_token(token)
    if meta.get("cancelled"):
        raise HTTPException(status_code=404, detail="Not found")
    return JSONResponse(do_notice(path, meta, str(data.get("mode", "")), ""), headers=HEADERS)


@router.post("/manage/{token}/api/reschedule")
async def manage_reschedule(token: str, request: Request):
    need_header(request)
    data = await request.json()
    path, meta = find_by_token(token)
    if meta.get("cancelled"):
        raise HTTPException(status_code=404, detail="Not found")
    return JSONResponse(do_reschedule(path, meta, str(data.get("when", "")), str(data.get("tz", ""))), headers=HEADERS)


@router.post("/manage/{token}/api/cancel")
async def manage_cancel(token: str, request: Request):
    need_header(request)
    path, meta = find_by_token(token)
    do_cancel(path, meta)
    return JSONResponse({"ok": True}, headers=HEADERS)


@router.post("/manage/{token}/api/add-invitee")
async def manage_add_invitee(token: str, request: Request):
    need_header(request)
    data = await request.json()
    path, meta = find_by_token(token)
    if meta.get("cancelled"):
        raise HTTPException(status_code=404, detail="Not found")
    email = str(data.get("email", "")).strip()
    if not EMAIL_OK.match(email) or len(email) > 200:
        raise HTTPException(status_code=400, detail="That does not look like an email address")
    att = meta.setdefault("attendees", [])
    if email.lower() in [a.lower() for a in att]:
        raise HTTPException(status_code=409, detail="That person is already invited")
    if len(att) >= MAX_ATTENDEES:
        raise HTTPException(status_code=409, detail="This meeting is full")
    try:
        import access as _acc
        blocked = _acc.denied(email, "")
    except Exception:
        blocked = False
    if blocked:
        raise HTTPException(status_code=403, detail="That address cannot be invited")
    try:
        send(email, "Invitation: " + meta["title"], _invite_body_with_files(meta, False), ics_for(meta),
             host=meta.get("host", ""), html=invite_html(meta, False))
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Could not send the invitation: " + str(exc)[:80])
    att.append(email)
    meta.setdefault("invited", []).append(email)
    path.write_text(json.dumps(meta, indent=2))
    return JSONResponse({"ok": True, "email": email}, headers=HEADERS)


@router.post("/manage/{token}/api/remove-invitee")
async def manage_remove_invitee(token: str, request: Request):
    need_header(request)
    data = await request.json()
    path, meta = find_by_token(token)
    if meta.get("cancelled"):
        raise HTTPException(status_code=404, detail="Not found")
    email = str(data.get("email", "")).strip().lower()
    if email == (meta.get("host") or "").lower():
        raise HTTPException(status_code=400, detail="The host cannot be removed")
    before = len(meta.get("attendees", []))
    was_invited = any(a.lower() == email for a in meta.get("invited", []))
    removed = [a for a in meta.get("attendees", []) if a.lower() == email]
    meta["attendees"] = [a for a in meta.get("attendees", []) if a.lower() != email]
    meta["invited"] = [a for a in meta.get("invited", []) if a.lower() != email]
    if len(meta["attendees"]) == before:
        raise HTTPException(status_code=404, detail="Not found")
    path.write_text(json.dumps(meta, indent=2))
    notified = False
    if was_invited and removed:
        try:
            send(removed[0], "No longer invited: " + meta["title"],
                 "You are no longer invited to " + meta["title"] + " on " + fmt_local(meta["start_utc"], meta["tz"])
                 + ".\n\nThe meeting link no longer applies to you, and the calendar entry has been removed.\n"
                 "If you think this is a mistake, please reply to this email.\n\nExample Org\n",
                 ics_for(meta, "CANCEL"), "CANCEL", host=meta.get("host", ""))
            notified = True
        except Exception:
            notified = False
    return JSONResponse({"ok": True, "notified": notified}, headers=HEADERS)
