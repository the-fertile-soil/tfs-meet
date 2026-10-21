"""Emails about finished meetings: the host-first review email and the final email to everyone.

Uses the same look as the invitation emails (invite_tpl.html header and footer).
"""
import html as _h
import json
import secrets
from datetime import datetime, timedelta
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path

import mailer
import docxout
import pdfout

UPLOADS = Path("/opt/tfs-review/uploads")
TPL = Path("/opt/tfs-review/invite_tpl.html")
PUBLIC_BASE = "https://meet.example.org"
MAIL_FROM = "Example Org Meetings <meetings@example.org>"
INBOX_ADDR = "transcripts@example.org"
REVIEW_HOURS = 6
RETENTION_DAYS = 30
F = "font-family:system-ui,-apple-system,'Segoe UI',Roboto,Arial,sans-serif;"


def _frame(title: str, inner: str) -> str:
    tpl = TPL.read_text()
    head = tpl[:tpl.index("<!-- card -->")]
    foot = "</td></tr>\n\n" + tpl[tpl.index("<!-- footer -->"):]
    card = ('<!-- card -->\n<tr><td bgcolor="#fffdf9" style="background:#fffdf9;border-left:1px solid #e6d9c8;'
            'border-right:1px solid #e6d9c8;padding:32px 32px 18px 32px;' + F + 'color:#2e2118;">\n' + inner + "\n")
    out = head + card + foot
    for k, v in {"{{TITLE}}": _h.escape(title), "{{SHORT}}": _h.escape(title), "{{ROOM}}": "", "{{MANAGE}}": ""}.items():
        out = out.replace(k, v)
    return out


def _button(label: str, href: str, primary: bool = True) -> str:
    if primary:
        return ('<table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin:10px 0;width:100%;"><tr>'
                '<td align="center" bgcolor="#5f7650" style="background:#5f7650;border-radius:999px;">'
                '<a href="' + _h.escape(href) + '" style="display:block;padding:14px 24px;' + F +
                'font-size:17px;font-weight:700;color:#ffffff;text-decoration:none;border-radius:999px;">' + _h.escape(label) + '</a></td></tr></table>')
    return ('<table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin:10px 0;width:100%;"><tr>'
            '<td align="center" style="border:2px solid #5f7650;border-radius:999px;">'
            '<a href="' + _h.escape(href) + '" style="display:block;padding:12px 24px;' + F +
            'font-size:16px;font-weight:700;color:#4a5e3d;text-decoration:none;border-radius:999px;">' + _h.escape(label) + '</a></td></tr></table>')


def _b(s: str) -> str:
    import re as _re
    return _re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", _h.escape(s))


def _divider() -> str:
    sp = '<div style="height:36px;line-height:36px;font-size:1px;">&nbsp;</div>'
    return (sp + '<table role="presentation" align="center" width="60%" cellpadding="0" cellspacing="0" border="0" style="width:60%;margin:0 auto;">'
            '<tr><td style="border-top:2px solid #b8a48a;font-size:1px;line-height:1px;">&nbsp;</td></tr></table>' + sp)


def _transcript_block(link: str) -> str:
    if not link:
        return ""
    return (_divider() +
            '<h3 style="margin:14px 0 6px 0;font-family:Georgia,\'Times New Roman\',serif;font-size:19px;color:#2e2118;">Transcript</h3>'
            + _button("Read the full transcript", link, primary=False))


def _host_link(meta: dict) -> str:
    host = (meta.get("host") or "").strip().lower()
    for k, v in (meta.get("reviews") or {}).items():
        if k.strip().lower() == host:
            return PUBLIC_BASE + "/review/" + v + "#transcript"
    return ""


def _md_to_html(md: str) -> str:
    out, in_ul = [], False
    for line in md.splitlines():
        s = line.strip()
        if s.startswith("## "):
            if in_ul:
                out.append("</ul>"); in_ul = False
            out.append('<h3 style="margin:18px 0 6px 0;font-family:Georgia,\'Times New Roman\',serif;font-size:19px;color:#2e2118;">' + _h.escape(s[3:]) + "</h3>")
        elif s.startswith(("- ", "* ")):
            if not in_ul:
                out.append('<ul style="margin:4px 0 8px 0;padding-left:22px;font-size:15px;line-height:1.55;color:#2e2118;">'); in_ul = True
            out.append("<li>" + _b(s[2:]) + "</li>")
        elif s:
            if in_ul:
                out.append("</ul>"); in_ul = False
            out.append('<p style="margin:0 0 8px 0;font-size:15px;line-height:1.55;color:#2e2118;">' + _b(s) + "</p>")
    if in_ul:
        out.append("</ul>")
    return "\n".join(out)


def _send(to_addr: str, subject: str, text: str, html: str, reply_to: str = "", attachments=None) -> None:
    msg = EmailMessage()
    msg["From"] = MAIL_FROM
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=False)
    msg["Message-ID"] = make_msgid(domain="example.org")
    msg["Auto-Submitted"] = "auto-generated"
    if reply_to:
        msg["Reply-To"] = reply_to
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    for name, data in (attachments or []):
        if name.lower().endswith(".pdf"):
            msg.add_attachment(data, maintype="application", subtype="pdf", filename=name)
        else:
            msg.add_attachment(data, maintype="text", subtype="plain", filename=name)
    mailer.send_message(msg)


def _expires(meta: dict) -> str:
    up = datetime.strptime(meta["uploaded_utc"], "%Y-%m-%dT%H:%M:%SZ")
    return (up + timedelta(days=RETENTION_DAYS)).strftime("%B %d, %Y").replace(" 0", " ")


def people_lines(meta: dict) -> list:
    if meta.get("people"):
        return list(meta["people"])
    host = (meta.get("host") or "").strip().lower()
    out = []
    for r in meta.get("recipients") or []:
        out.append(r + (" (host)" if r.strip().lower() == host else ""))
    return out


def _info(meta: dict) -> str:
    bits = []
    if meta.get("audio_seconds"):
        bits.append(str(round(meta["audio_seconds"] / 60)) + " minutes")
    return " ".join(bits) + ("." if bits else "") if bits else ""


def _title(meta: dict) -> str:
    return meta.get("title") or "your TFS meeting"


def send_host_review(meta: dict, revised: bool = False) -> None:
    """Email the host the transcript and summary and ask for corrections. Raises on failure."""
    host = meta["host"]
    tok = meta.get("host_token") or secrets.token_urlsafe(6)
    meta["host_token"] = tok
    title = _title(meta)
    tpath = UPLOADS / meta["transcript"]
    spath = UPLOADS / meta["summary"]
    transcript = tpath.read_text()
    summary = spath.read_text() if spath.exists() else ""
    if revised and meta.get("review_deadline_utc"):
        deadline = datetime.strptime(meta["review_deadline_utc"], "%Y-%m-%dT%H:%M:%SZ")
    else:
        deadline = datetime.utcnow() + timedelta(hours=REVIEW_HOURS)
        meta["review_deadline_utc"] = deadline.strftime("%Y-%m-%dT%H:%M:%SZ")
    subject = ("Updated: " if revised else "") + "Review the summary and transcript: " + title + " [TFS-" + tok + "]"
    from zoneinfo import ZoneInfo
    tzname = "America/Los_Angeles"
    try:
        sj = json.loads((Path("/opt/tfs-review/schedules") / (meta["file"].split("_")[-1].split(".")[0] + ".json")).read_text())
        tzname = sj.get("tz") or tzname
    except Exception:
        pass
    local = deadline.replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo(tzname))
    when_txt = local.strftime("%A, %B ") + str(local.day) + local.strftime(" at ") + str(int(local.strftime("%I"))) + local.strftime(":%M %p %Z")
    changes = [str(c) for c in (meta.get("host_changes_last") or [])] if revised else []
    changes_html = ""
    if revised:
        changes_html = ('<div style="padding:0 0 14px 0;"><strong style="color:#a8481f;">We updated the summary and transcript with your changes.</strong>'
                        + ('<ul style="margin:8px 0 0 0;padding-left:20px;">' + "".join("<li>" + _h.escape(c) + "</li>" for c in changes) + "</ul>" if changes else "")
                        + '<div style="padding-top:8px;">You can reply again with more changes.</div></div>')
    final_html = ""
    final_text = ""
    if meta.get("review_extensions", 0) >= 2:
        final_text = "This is your single, final chance to submit updates. Please make sure all changes are submitted before " + when_txt + ".\n\n"
        final_html = ('<div style="margin:0 0 16px 0;padding:14px 16px;background:#fff4cc;border:2px solid #c0562b;border-radius:10px;font-size:16px;line-height:1.5;color:#2e2118;">'
                      '<strong>This is your single, final chance to submit updates.</strong> Please make sure all changes are submitted before <strong>' + _h.escape(when_txt) + '</strong>.</div>')
    steps = (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin:6px 0 18px 0;background:#f4eadc;'
        'border:1px solid #e6d9c8;border-radius:12px;"><tr><td style="padding:18px 22px;' + F + 'font-size:16px;line-height:1.6;color:#2e2118;">'
        + changes_html + final_html +
        '<em>No action on your part is required.</em><div style="padding-top:12px;">If you do want to make any changes, please reply to this email with the modification either in an attached docx document or in the body of the email. We will incorporate all updates.</div>'
        '<div style="padding-top:12px;">We will send an email of the summary and transcript of the meeting to all participants on</div>'
        '<div style="padding:6px 0 0 0;font-family:Georgia,\'Times New Roman\',serif;font-size:18px;line-height:1.3;font-weight:700;color:#a8481f;">' + _h.escape(when_txt) + '</div>'
        '</td></tr></table>'
    )
    inner = (
        '<div style="font-size:12px;letter-spacing:2px;text-transform:uppercase;color:#c0562b;font-weight:700;">Host review</div>'
        '<h1 style="margin:8px 0 6px 0;font-family:Georgia,\'Times New Roman\',serif;font-size:30px;line-height:1.15;color:#2e2118;font-weight:700;">' + _h.escape(title) + '</h1>'
        '<p style="margin:0 0 18px 0;font-size:16px;line-height:1.55;color:#6b5a4b;">The summary and transcript of your meeting are ready. As the host, you get to see them first to review everything for accuracy.</p>'
        + steps +
        '<h2 style="margin:34px 0 4px 0;font-family:Georgia,\'Times New Roman\',serif;font-size:22px;color:#2e2118;">Draft summary</h2>' + _md_to_html(summary) + _transcript_block(_host_link(meta))
    )
    text = (("Hello,\n\nWe updated the summary and transcript with your changes.\n\n" if revised else "Hello,\n\n") + final_text + "The summary and transcript of " + title + " are ready and attached as one Word file. As the host, you get to see them first to review everything for accuracy.\n\n"
            "No action on your part is required.\n\nIf you do want to make any changes, please reply to this email with the modification either in an attached docx document or in the body of the email. We will incorporate all updates.\n\n"
            "We will send an email of the summary and transcript of the meeting to all participants on " + when_txt + "\n\n"
            "DRAFT SUMMARY\n\n" + summary + "\nExample Org\n")
    _send(host, subject, text, _frame("Review the transcript: " + title, inner), reply_to=INBOX_ADDR,
          attachments=[(title.replace("/", "-").replace("&amp;", "and")[:60] + " Summary and transcript.docx",
                        docxout.make_docx(_h.unescape(title), meta["uploaded_utc"][:10], summary, transcript,
                                          note="HOST: edit anything below, the summary and action items as well as the transcript, then reply to the email with this file attached. Replace Speaker 1, Speaker 2 and so on with real names.",
                                          info=_info(meta), people=people_lines(meta)))])


def send_everyone_one(meta: dict, addr: str, token: str, reviewed: bool = False) -> None:
    title = _title(meta)
    base = PUBLIC_BASE + "/review/" + token
    spath = UPLOADS / meta["summary"] if meta.get("summary") else None
    summary = spath.read_text() if spath and spath.exists() else ""
    tpath = UPLOADS / meta["transcript"] if meta.get("transcript") else None
    inner = (
        '<div style="font-size:12px;letter-spacing:2px;text-transform:uppercase;color:#c0562b;font-weight:700;">Meeting notes</div>'
        '<h1 style="margin:8px 0 6px 0;font-family:Georgia,\'Times New Roman\',serif;font-size:30px;line-height:1.15;color:#2e2118;font-weight:700;">' + _h.escape(title) + '</h1>'
        '<p style="margin:0 0 18px 0;font-size:16px;line-height:1.55;color:#6b5a4b;">Here is everything from the meeting: the summary, the full transcript (attached as a PDF), and the recording.</p>'
        + _button("Download the video", base + "/download") +
        _button("Watch and read it online", base, primary=False) +
        '<p style="margin:2px 0 16px 0;font-size:13px;line-height:1.5;color:#6b5a4b;text-align:center;">The video downloads straight to your device when you tap the first button. '
        'Notes as a PDF: <a href="' + base + '/pdf" style="color:#4a5e3d;font-weight:700;">download the PDF</a></p>'
        + _divider() + _md_to_html(summary) + _transcript_block(base + "#transcript") +
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin:22px 0 4px 0;background:#f4eadc;border:1px solid #e6d9c8;border-radius:12px;"><tr><td style="padding:16px 20px;' + F + 'font-size:15px;line-height:1.6;color:#2e2118;">'
        'As a participant in <strong>' + _h.escape(title) + '</strong>, you have access to review this media for ' + str(RETENTION_DAYS) + ' days, until <strong>' + _h.escape(_expires(meta)) + '</strong>.</td></tr></table>'
    )
    text = ("Hello,\n\nHere is everything from " + title + ".\n\nDownload the video (starts downloading right away):\n" + base + "/download\n\n"
            "Watch and read it online:\n" + base + "\n\nNotes as a PDF:\n" + base + "/pdf\n\n"
            "SUMMARY\n\n" + summary + "\n\n"
            "As a participant in " + title + ", you have access to review this media for " + str(RETENTION_DAYS) + " days, until " + _expires(meta) + ".\n\nExample Org\n")
    att = []
    if tpath and tpath.exists():
        att.append((_h.unescape(title).replace("/", "-")[:60] + " notes and transcript.pdf",
                    pdfout.make_pdf(_h.unescape(title), meta["uploaded_utc"][:10], summary, tpath.read_text(), people_lines(meta))))
    _send(addr, "Meeting notes: " + title, text, _frame("Meeting notes: " + title, inner), attachments=att)
