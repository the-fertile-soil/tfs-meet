"""Email people on the waitlist when a slot becomes free. Runs every minute (systemd timer)."""
import json
import time
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

import availability
import mailer

FROM = "Example Org Meetings <meetings@example.org>"
REPLY = "meetings-admin@example.org"

try:
    items = json.loads(availability.WAITLIST.read_text())
except Exception:
    raise SystemExit(0)
st = availability.status()
if not st["known"]:
    raise SystemExit(0)
now = time.time()
keep, sent = [], 0
for i in items:
    if now - i["added"] > 86400:
        continue
    ok = st["video"]["available"] if i["kind"] == "video" else st["audio"]["free"] > 0
    if not ok:
        keep.append(i)
        continue
    what = "a video session (up to 8 people, up to 90 minutes)" if i["kind"] == "video" else "an audio-only session"
    msg = EmailMessage()
    msg["From"] = FROM
    msg["To"] = i["email"]
    msg["Reply-To"] = REPLY
    msg["Subject"] = "A meeting slot is open now"
    msg["Date"] = formatdate(localtime=False)
    msg["Message-ID"] = make_msgid(domain="example.org")
    msg["Auto-Submitted"] = "auto-generated"
    msg.set_content(
        "Good news: " + what + " is open right now on Example Org's free meeting service.\n\n"
        "Start it here: https://example.org/meetings/\n\n"
        "Slots go to whoever starts first, so please act soon. Please note that all meetings are recorded, "
        "saved, transcribed, and summarized by AI.\n\n"
        "This is a one-time message. You are no longer on the waitlist.\n")
    try:
        mailer.send_message(msg)
        sent += 1
    except Exception as e:
        print("mail failed", e)
        keep.append(i)
availability.WAITLIST.write_text(json.dumps(keep))
if sent:
    print("notified", sent)
