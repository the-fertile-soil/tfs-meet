"""Locked-down admin endpoints so a session without server access can check health, read logs and send a test.

Every call needs the long token in the Authorization header. Nothing here runs arbitrary commands.
"""
import hmac
import json
import shutil
import subprocess
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse

router = APIRouter(prefix="/api/admin")
TOKEN = Path("/opt/tfs-review/admin.token")
UPLOADS = Path("/opt/tfs-review/uploads")
UNITS = {"tfs-review", "tfs-process", "tfs-ingest", "tfs-inbox", "tfs-quiet-watch", "tfs-purge", "tfs-backup", "tfs-waitlist"}
TIMERS = ["tfs-ingest", "tfs-process", "tfs-quiet-watch", "tfs-inbox", "tfs-purge", "tfs-backup", "tfs-waitlist"]
TEST_HOST = "admin@example.org"
_fails = []


def _auth(request: Request) -> None:
    now = time.time()
    _fails[:] = [t for t in _fails if now - t < 60]
    if len(_fails) >= 10:
        raise HTTPException(status_code=429, detail="Too many attempts. Wait a minute.")
    try:
        good = TOKEN.read_text().strip()
    except Exception:
        raise HTTPException(status_code=503, detail="Admin is not set up.")
    sent = request.headers.get("authorization", "")
    sent = sent[7:].strip() if sent.lower().startswith("bearer ") else ""
    if not good or not hmac.compare_digest(sent.encode(), good.encode()):
        _fails.append(now)
        time.sleep(1)
        raise HTTPException(status_code=401, detail="Not allowed.")


def _run(args, timeout=20) -> str:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout).stdout.strip()
    except Exception as exc:
        return "error: " + str(exc)[:100]


@router.get("/health")
async def health(request: Request):
    _auth(request)
    queue = {}
    problems = []
    for p in UPLOADS.glob("*.json"):
        try:
            m = json.loads(p.read_text())
        except Exception:
            continue
        st = m.get("status", "?")
        queue[st] = queue.get(st, 0) + 1
        if st == "failed":
            problems.append(m.get("file", p.name) + ": " + str(m.get("error", ""))[:120])
    timers = {t: _run(["systemctl", "is-active", t + ".timer"]) for t in TIMERS}
    svc = {"tfs-review": _run(["systemctl", "is-active", "tfs-review"])}
    for t in ("tfs-inbox", "tfs-process"):
        svc[t + "_last_result"] = _run(["systemctl", "show", t, "-p", "Result", "--value"])
    d = shutil.disk_usage("/")
    mem = _run(["free", "-m"]).splitlines()[1].split() if _run(["free", "-m"]) else []
    return JSONResponse({
        "time_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "service": svc, "timers": timers, "queue": queue, "failed_jobs": problems,
        "disk_free_gb": round(d.free / 1e9, 1), "mem_available_mb": int(mem[6]) if len(mem) > 6 else None,
        "containers": _run(["docker", "ps", "--format", "{{.Names}}: {{.Status}}"]).splitlines(),
    })


@router.get("/logs")
async def logs(request: Request, unit: str = "tfs-process", n: int = 100):
    _auth(request)
    if unit not in UNITS:
        raise HTTPException(status_code=400, detail="Unknown unit.")
    n = max(1, min(n, 400))
    return PlainTextResponse(_run(["journalctl", "-u", unit, "-n", str(n), "--no-pager", "-o", "short"], 20))


@router.post("/process-now")
async def process_now(request: Request):
    _auth(request)
    _run(["systemctl", "start", "tfs-process", "--no-block"])
    return {"ok": True}


@router.post("/test-host-review")
async def test_host_review(request: Request):
    """Make a fictitious meeting whose only person is the owner, and run it through the host review email."""
    _auth(request)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    f = stamp + "_tfs-test" + stamp[-4:] + ".mp4"
    (UPLOADS / f).write_bytes(b"test")
    tr = ("[00:00:05] Alex: Welcome everyone. Today we need to decide how to launch the member portal and who does what.\n"
          "[00:00:20] Sam: I think option B is the right one. It is simpler for members and we can have it ready quickly.\n"
          "[00:00:41] Jordan: Agreed. I have six mentors ready for a pilot, but we still need to decide who pays for the background checks.\n"
          "[00:01:05] Alex: Good question. Let us leave that open for now. Sam, can you send the draft member survey to the group by Friday?\n"
          "[00:01:20] Sam: Yes, I will send it Friday.\n"
          "[00:01:30] Alex: Jordan, please confirm the pilot mentors by next Monday. I will set up the sign-up page by the end of the month.\n"
          "[00:01:48] Jordan: Will do. So we are going with option B and launching in the first week of November.\n"
          "[00:02:00] Alex: Yes, that is the decision. Thanks everyone.\n")
    (UPLOADS / (f + ".transcript.txt")).write_text(tr)
    m = {"file": f, "title": "TEST member portal launch", "bytes": 4, "uploaded_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
         "audio_seconds": 125.0, "recipients": [TEST_HOST], "host": TEST_HOST, "keep": False, "status": "transcribed",
         "source": "jibri", "kind": "video", "language": "en", "transcript": f + ".transcript.txt",
         "reviews": {TEST_HOST: "testtoken-" + stamp + "-abcdefghijklmnopqrstuvwxyz"}}
    (UPLOADS / (f + ".json")).write_text(json.dumps(m, indent=2))
    _run(["systemctl", "start", "tfs-process", "--no-block"])
    return {"ok": True, "meeting": f, "sends_to": TEST_HOST}
