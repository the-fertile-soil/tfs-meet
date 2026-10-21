"""Send mail through Example Org's own mail server (authenticated submission)."""
import json
import smtplib
import ssl
from pathlib import Path

CFG = Path("/opt/tfs-review/mail.json")


def send_message(msg, from_addr="meetings@example.org") -> None:
    cfg = json.loads(CFG.read_text())
    ctx = ssl.create_default_context()
    with smtplib.SMTP(cfg["host"], int(cfg["port"]), timeout=60) as smtp:
        smtp.starttls(context=ctx)
        smtp.login(cfg["user"], cfg["password"])
        smtp.send_message(msg, from_addr=from_addr)
