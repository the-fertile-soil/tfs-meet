"""Read replies to the host-review email from the transcripts@ mailbox.

A reply counts when it comes from the meeting's host, carries the [TFS-token] in the subject, and holds
a corrected transcript (a .txt/.md/.docx attachment, or at least 100 words typed above the quoted text).
The corrected transcript replaces the old one and the meeting goes back through summarizing and sending.
"""
import imaplib
import io
import json
import re
import sys
import zipfile
from email import message_from_bytes
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr
from pathlib import Path

sys.path.insert(0, "/opt/tfs-review")
import docxout
import mailer
import notify
import quiet
import urllib.request
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime

UPLOADS = Path("/opt/tfs-review/uploads")
PASS = Path("/root/.secrets/transcripts.pass")
HOST, USER = "mail.example.org", "transcripts@example.org"
TOKEN_RE = re.compile(r"\[TFS-([A-Za-z0-9_-]{6,20})\]")
QUOTE_START = re.compile(r"^(On .{5,200}wrote:|-{2,}\s*Original Message\s*-{2,}|From:\s.+@.+|_{5,})\s*$", re.I)


def now_utc() -> str:
    import time
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def docx_text(data: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        xml = z.read("word/document.xml").decode("utf8", "ignore")
    xml = re.sub(r"</w:p>", "\n", xml)
    xml = re.sub(r"<w:tab/>", "\t", xml)
    xml = re.sub(r"<[^>]+>", "", xml)
    import html
    return html.unescape(xml)


def body_text(msg) -> str:
    plain = None
    for part in msg.walk():
        if part.get_content_type() == "text/plain" and not part.get_filename():
            plain = part.get_payload(decode=True).decode(part.get_content_charset() or "utf8", "ignore")
            break
    if plain is None:
        return ""
    keep = []
    for line in plain.splitlines():
        if QUOTE_START.match(line.strip()) or line.startswith(">"):
            break
        keep.append(line)
    return "\n".join(keep).strip()


def pages_text(data: bytes) -> str:
    import zipfile, io, snappy
    z = zipfile.ZipFile(io.BytesIO(data))
    def iwa(d):
        out = b""; i = 0
        while i < len(d):
            if d[i] != 0: break
            ln = int.from_bytes(d[i+1:i+4], "little"); i += 4
            out += snappy.uncompress(d[i:i+ln]); i += ln
        return out
    blobs = []
    names = z.namelist()
    if "Index.zip" in names:
        inner = zipfile.ZipFile(io.BytesIO(z.read("Index.zip")))
        blobs = [iwa(inner.read(n)) for n in inner.namelist() if n.endswith(".iwa")]
    else:
        blobs = [iwa(z.read(n)) for n in names if n.endswith(".iwa")]
    best = ""
    for b in blobs:
        for m in re.finditer(rb"(?:[\x09\x0a\x0d\x20-\x7e]|[\xc2-\xf4][\x80-\xbf]{1,3}){200,}", b):
            x = m.group(0).decode("utf8", "ignore")
            if len(x) > len(best): best = x
    out = []
    for ln in best.splitlines():
        mm = re.match(r"^(Speaker \d+)\s+(\d\d:\d\d:\d\d)\s*$", ln)
        if mm: out.append("[" + mm.group(2) + "] " + mm.group(1) + ": ")
        elif ln.strip() and ln.strip() != "Transcript":
            if out: out[-1] += ln.strip() + " "
            else: out.append(ln.strip())
    return "\n".join(x.strip() for x in out)


def find_text(msg):
    """Return (summary_markdown_or_empty, transcript_text, people_lines)."""
    for part in msg.walk():
        fn = (part.get_filename() or "").lower()
        if not fn:
            continue
        data = part.get_payload(decode=True) or b""
        try:
            if fn.endswith((".txt", ".md")):
                t = data.decode("utf8", "ignore")
            elif fn.endswith(".docx"):
                s_md, t, ppl = docxout.parse_docx(data)
                if not t or len(t.split()) < 30:
                    t = docx_text(data)
                    s_md, ppl = "", []
                return s_md, t.strip(), ppl
            elif fn.endswith(".pages"):
                t = pages_text(data)
            else:
                continue
        except Exception:
            continue
        if len(t.split()) >= 30:
            return "", t.strip(), []
    b = body_text(msg)
    looks = len(re.findall(r"(?m)^\s*(\[?\d\d:\d\d(:\d\d)?\]?|Speaker \d+)", b)) >= 5
    return "", (b if (len(b.split()) >= 100 and looks) else ""), []


def reply_inner(text: str, heading: str = "Thank you") -> str:
    import html as _h
    body = text.replace("\n\nExample Org\n", "").strip()
    paras = [p.strip() for p in body.split("\n\n") if p.strip()]
    if paras and paras[0].startswith("Thank you. "):
        paras[0] = paras[0][len("Thank you. "):]
    elif paras and paras[0].startswith("Thanks. "):
        paras[0] = paras[0][len("Thanks. "):]
    parts = []
    for p in paras:
        e = _h.escape(p)
        if p.startswith(("Everyone else will get", "The deadline has been extended", "The final summary will be sent")):
            e = re.sub(r"(on|before) ([A-Z][a-z]+day, [A-Z][a-z]+ \d+ at \d+:\d\d [AP]M [A-Z]{3,4})",
                       r'\1<div style="padding-top:6px;font-family:Georgia,Times New Roman,serif;font-size:20px;line-height:1.3;font-weight:700;color:#a8481f;">\2</div>', e)
            parts.append('<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin:6px 0 18px 0;background:#f4eadc;'
                         'border:1px solid #e6d9c8;border-radius:12px;"><tr><td style="padding:18px 22px;' + notify.F + 'font-size:16px;line-height:1.6;color:#2e2118;">' + e + '</td></tr></table>')
        elif p.startswith("This is your single, final chance"):
            parts.append('<div style="margin:0 0 16px 0;padding:14px 16px;background:#fff4cc;border:2px solid #c0562b;border-radius:10px;font-size:16px;line-height:1.5;color:#2e2118;"><strong>' + e + '</strong></div>')
        else:
            e = re.sub(r"(https://[^\s<]+)", r'<a href="\1" style="color:#a8481f;font-weight:700;">\1</a>', e)
            parts.append('<p style="margin:0 0 16px 0;font-size:16px;line-height:1.6;color:#2e2118;">' + e + '</p>')
    return ('<div style="font-size:12px;letter-spacing:2px;text-transform:uppercase;color:#c0562b;font-weight:700;">Host review</div>'
            '<h1 style="margin:8px 0 14px 0;font-family:Georgia,\'Times New Roman\',serif;font-size:30px;line-height:1.15;color:#2e2118;font-weight:700;">' + _h.escape(heading) + '</h1>'
            + "".join(parts))


def reply(to_addr: str, subject: str, text: str, heading: str = "Thank you") -> None:
    """Branded acknowledgement email, same look as the other Example Org emails."""
    notify._send(to_addr, " ".join(str(subject).split()), text, notify._frame(heading, reply_inner(text, heading)), reply_to=notify.INBOX_ADDR)

class Defer(Exception):
    """Leave the message unread and try again on the next run."""


OLLAMA = "http://127.0.0.1:11434/api/generate"
NAME_RE = re.compile(r"[A-Za-z][A-Za-z.' -]{0,40}")
EDIT_PROMPT = (
    "A meeting host sent corrections about the transcript of a meeting. Read the host's message and list the changes it asks for. "
    "Reply with JSON only, in exactly this shape: "
    '{"rename_speakers": [{"from": "name now used in the transcript", "to": "the correct full name"}], '
    '"add_people": ["name of a person who attended but is not on the people list"], '
    '"other": ["any other correction, one short sentence each"]}. '
    "Use empty lists when there is nothing. Only include what the host actually said.\n\n"
)


def ask_edits(instructions: str, known: list) -> dict:
    prompt = EDIT_PROMPT + "NAMES CURRENTLY IN THE TRANSCRIPT: " + (", ".join(known) or "none") + "\n\nHOST MESSAGE:\n" + instructions[:3000]
    body = json.dumps({"model": "qwen2.5:7b", "prompt": prompt, "stream": False, "format": "json",
                       "options": {"temperature": 0, "num_ctx": 4096, "num_predict": 500}}).encode()
    req = urllib.request.Request(OLLAMA, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=900) as resp:
        raw = json.loads(resp.read())["response"]
    m = re.search(r"\{.*\}", raw, re.S)
    return json.loads(m.group(0)) if m else {}


def known_names(text: str) -> list:
    return sorted(set(re.findall(r"Speaker \d+ \(([^)]+)\)", text)))


def apply_instructions(meta: dict, tpath: Path, instructions: str) -> list:
    """Use the AI to read the host's note, apply the clear edits, and remember the note for the new summary."""
    text = tpath.read_text()
    changes = []
    notes = []
    try:
        data = ask_edits(instructions, known_names(text))
    except Exception as exc:
        print("edit extraction failed", repr(exc), flush=True)
        data = {}
    for item in data.get("rename_speakers", []) or []:
        a, b = str((item or {}).get("from", "")).strip(), str((item or {}).get("to", "")).strip()
        if not (NAME_RE.fullmatch(a) and NAME_RE.fullmatch(b)) or a.lower() == b.lower():
            continue
        text, n = re.subn(r"\b" + re.escape(a) + r"\b", b, text)
        people = [re.sub(r"\b" + re.escape(a) + r"\b", b, p) for p in notify.people_lines(meta)]
        meta["people"] = people
        notes.append(a + " is actually " + b)
        changes.append("The speaker labelled " + a + " is now " + b + (" (" + str(n) + " places in the transcript)" if n else ""))
    adds = []
    for name in data.get("add_people", []) or []:
        name = str(name).strip()
        if NAME_RE.fullmatch(name):
            adds.append(name)
    if adds:
        people = notify.people_lines(meta)
        for name in adds:
            if not any(name.lower() in p.lower() for p in people):
                people.append(name + " (attended, email not on file)")
        meta["people"] = people
        notes.append("Also present, not on the people list: " + ", ".join(adds))
        changes.append("Added to the people list: " + ", ".join(adds))
    try:
        import names as _nm
        canon = list(adds) + [str((i or {}).get("to", "")) for i in data.get("rename_speakers", []) or []]
        canon += _nm.names_from_note(instructions)
        canon += [x.get("name", "") for x in (meta.get("session") or {}).get("people", [])]
        text, fixed = _nm.fix_names(text, canon)
        for a, b, n in fixed:
            changes.append("Fixed the spelling of " + a + " to " + b + " (" + str(n) + " place" + ("" if n == 1 else "s") + " in the transcript)")
    except Exception as exc:
        print("name fix failed", repr(exc), flush=True)
    for o in data.get("other", []) or []:
        o = str(o).strip()
        if o:
            notes.append(o)
            changes.append(o)
    if not notes:
        notes.append(" ".join(instructions.split())[:600])
        changes.append("Your note was passed to the AI that rewrites the summary")
    tpath.write_text(text)
    meta.setdefault("host_notes", []).extend(notes)
    meta["host_notes_raw"] = (meta.get("host_notes_raw") or []) + [" ".join(instructions.split())[:1500]]
    return changes


def extend_deadline(meta: dict, when) -> bool:
    """A reply in the last hour before the final send pushes it back one hour, up to 3 extra hours in all."""
    n = meta.get("review_extensions", 0)
    dl = datetime.strptime(meta["review_deadline_utc"], "%Y-%m-%dT%H:%M:%SZ")
    if n < 3 and dl - when <= timedelta(hours=1):
        meta["review_deadline_utc"] = (dl + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        meta["review_extensions"] = n + 1
        return True
    return False


def send_time_text(meta: dict) -> str:
    from zoneinfo import ZoneInfo
    dl = datetime.strptime(meta["review_deadline_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo("America/Los_Angeles"))
    return dl.strftime("%A, %B ") + str(dl.day) + dl.strftime(" at ") + str(int(dl.strftime("%I"))) + dl.strftime(":%M %p %Z")


def build_ack(meta: dict, final_mode: bool) -> str:
    when = send_time_text(meta)
    if final_mode:
        return ("Thank you. All of your changes will be incorporated.\n\n"
                "The final summary will be sent to all participants on " + when + ".")
    head = "Thank you. All of your changes will be incorporated.\n\nYou will receive another updated summary shortly.\n\n"
    if meta.get("review_extensions", 0) <= 1:
        return head + "The deadline has been extended one hour. You have a total of two more chances to revise the data before " + when + "."
    return head + ("The deadline has been extended one hour. If you wish to submit more revisions, please note that you will receive an "
                   "additional hour before the deadline is reached and you will have a single, final chance to make any updates before " + when + ".")


def handle(box, num) -> None:
    typ, raw = box.fetch(num, "(BODY.PEEK[])")
    msg = message_from_bytes(raw[0][1])
    if (msg.get("Auto-Submitted", "no").lower() != "no") or "bulk" in (msg.get("Precedence", "").lower()):
        return
    m = TOKEN_RE.search(msg.get("Subject", "") or "")
    sender = parseaddr(msg.get("From", ""))[1].lower()
    if not m or not sender:
        return
    for mp in UPLOADS.glob("*.json"):
        try:
            meta = json.loads(mp.read_text())
        except Exception:
            continue
        if meta.get("host_token") != m.group(1):
            continue
        subj = "Re: " + (msg.get("Subject") or "")
        if sender != (meta.get("host") or "").lower():
            print("ignored reply from non-host", sender, flush=True)
            break
        try:
            sent_at = parsedate_to_datetime(msg["Date"]).astimezone(__import__("datetime").timezone.utc).replace(tzinfo=None)
        except Exception:
            sent_at = datetime.utcnow()
        late = sent_at > datetime.strptime(meta.get("review_deadline_utc", "9999-12-31T00:00:00Z"), "%Y-%m-%dT%H:%M:%SZ")
        if meta.get("host_third_used") and not meta.get("emailed_utc") and not late:
            reply(sender, subj, "Thanks. You have already submitted all three rounds of changes, so this reply was not used.\n\n"
                  "The final summary will be sent to all participants on " + send_time_text(meta) + ".\n\nExample Org\n")
            break
        if meta.get("emailed_utc") or meta.get("host_final_ready") or late:
            link = notify._host_link(meta).split("#")[0]
            reply(sender, subj, "Thanks. The review window for this meeting has closed and its notes have gone to everyone."
                  + ("\n\nYou can review the meeting data here: " + link if link else "\n\nYou can review the meeting data in the email that was sent to everyone.")
                  + "\n\nExample Org\n")
            break
        if meta.get("status") in ("transcribing", "summarizing") or quiet.meetings_live():
            raise Defer()
        new_summary, text, new_people = find_text(msg)
        instructions = "" if text else body_text(msg)
        if not text and len(instructions.split()) < 3:
            reply(sender, subj,
                  "Thanks for your reply, but we did not find any changes in it.\n\n"
                  "Please reply again and write the changes at the top of your reply (above any quoted text), for example who a speaker really is "
                  "or who was missing from the people list. You can also attach the corrected transcript as a .txt or .docx file.\n\nExample Org\n")
            break
        tpath = UPLOADS / meta["transcript"]
        orig = tpath.with_name(tpath.name.replace(".transcript.txt", ".transcript.original.txt"))
        if not orig.exists():
            orig.write_text(tpath.read_text())
        changes = []
        if text:
            tpath.write_text(text.rstrip() + "\n")
            changes.append("Your corrected transcript replaced the previous one")
            if new_people:
                meta["people"] = new_people
        else:
            changes = apply_instructions(meta, tpath, instructions)
        meta["status"] = "transcribed"
        spath = UPLOADS / meta["summary"] if meta.get("summary") else None
        old_summary = spath.read_text() if spath and spath.exists() else ""
        if new_summary and spath and docxout.norm(new_summary) != docxout.norm(old_summary):
            sorig = spath.with_name(spath.name + ".original")
            if not sorig.exists():
                sorig.write_text(old_summary)
            spath.write_text(new_summary.rstrip() + "\n")
            meta["summary_edited_by_host"] = True
            meta["status"] = "done"
            changes.append("Your edited summary was kept as written")
        else:
            meta["summary_edited_by_host"] = False
        final_mode = meta.get("review_extensions", 0) >= 2
        if not final_mode:
            dl = datetime.strptime(meta["review_deadline_utc"], "%Y-%m-%dT%H:%M:%SZ")
            meta["review_deadline_utc"] = (dl + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
            meta["review_extensions"] = meta.get("review_extensions", 0) + 1
        else:
            meta["host_third_used"] = True
        meta["host_reviewed"] = True
        meta["host_reviewed_utc"] = now_utc()
        meta["host_replies"] = meta.get("host_replies", 0) + 1
        meta["host_changes_last"] = changes
        meta["host_revision_pending"] = not final_mode
        meta["host_final_ready"] = False
        meta["host_revision_attempts"] = 0
        mp.write_text(json.dumps(meta, indent=2))
        ack = build_ack(meta, final_mode)
        reply(sender, subj, ack + "\n\nExample Org\n")
        extended = not final_mode
        print("accepted host changes for", meta.get("file"), "extended" if extended else "", flush=True)
        break


def main() -> int:
    if not PASS.exists():
        return 0
    box = imaplib.IMAP4_SSL(HOST, 993)
    box.login(USER, PASS.read_text().strip())
    box.select("INBOX")
    typ, data = box.search(None, "UNSEEN")
    for num in (data[0].split() if data and data[0] else []):
        try:
            handle(box, num)
            box.store(num, "+FLAGS", "\\Seen")
        except Defer:
            print("deferred message", num, flush=True)
        except Exception as exc:
            print("error handling message", num, repr(exc), flush=True)
            box.store(num, "+FLAGS", "\\Seen")
    box.logout()
    return 0


if __name__ == "__main__":
    sys.exit(main())
