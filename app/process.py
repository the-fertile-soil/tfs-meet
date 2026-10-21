"""TFS meeting processor: transcribes and summarizes uploaded recordings, one at a time.

Run by tfs-process.timer. Status flow in each upload sidecar JSON:
  uploaded -> transcribing -> transcribed -> summarizing -> done   (or failed)
Outputs next to the recording:
  <file>.transcript.txt   timestamped transcript
  <file>.summary.md       AI summary (summary, key points, decisions, action items)
"""
import fcntl
import json
import re
import secrets
import smtplib
import subprocess
import sys
import time
import traceback
import urllib.request
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path

import numpy as np
from faster_whisper import WhisperModel

import diarize
import mailer
import notify
import quiet

UPLOADS = Path("/opt/tfs-review/uploads")
MODELS = "/opt/tfs-review/models"
LOCK = Path("/opt/tfs-review/.process.lock")
OLLAMA = "http://127.0.0.1:11434/api/generate"
LLM = "qwen2.5:7b"
WHISPER_SIZE = "medium"
CHUNK_CHARS = 12000
PUBLIC_BASE = "https://meet.example.org"
MAIL_FROM = "Example Org Meetings <meetings@example.org>"
RETENTION_DAYS = 30
MAX_EMAIL_ATTEMPTS = 3

PROMPT_CHUNK = (
    "You are taking detailed notes for a nonprofit meeting. Below is part of an automatic transcript. "
    "It may contain recognition errors, and the speaker labels (Speaker 1, Speaker 2, ...) are unreliable, "
    "but use the names people say and keep who said what whenever it is clear. Write notes under these headings:\n"
    "TOPICS: each topic with the specifics discussed (facts, numbers, options, reasons) and who raised them.\n"
    "DECISIONS: anything the group agreed, close to the words used.\n"
    "TASKS: each task someone agreed to do: who, what, and the deadline exactly as spoken (for example 'by next Monday').\n"
    "QUESTIONS: questions left open or unanswered.\n"
    "Introductions and casual conversation still count, so note who met and what was shown or talked about. "
    "Use only what is in the transcript. Do not invent anything. "
    "Only if the text is empty noise, reply: Nothing substantive.\n\nTRANSCRIPT:\n"
)

PROMPT_FINAL = (
    "You are writing the meeting summary for a nonprofit called Example Org. Below are "
    "notes taken from an automatic transcript, which may have recognition errors. Write the "
    "summary in exactly this format using Markdown, with these five headings in this order:\n\n"
    "## At a glance\nThree to five sentences: what the meeting was for, who took part when names are known, "
    "what was settled, and what happens next.\n\n"
    "## Decisions\nBullet list, one decision per bullet, stated plainly. Or the single line: None recorded.\n\n"
    "## Action items\nOne bullet per task in this form: Name: what they will do (due when it was said). "
    "Use Someone when no owner was named. Keep deadlines exactly as spoken and never work out calendar dates. "
    "Or the single line: None recorded.\n\n"
    "## Open questions\nBullet list of questions or issues left unresolved. Or the single line: None recorded.\n\n"
    "## Discussion by topic\nOne bullet per topic in this form: **Topic name:** two to four sentences with the "
    "specifics, including who raised what.\n\n"
    "Merge duplicate items. Use only what is in the notes. Do not invent names, dates, or facts. "
    "Do not say there was no discussion unless every note says Nothing substantive.\n\nNOTES:\n"
)


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def load_audio(path: Path) -> np.ndarray:
    cmd = ["ffmpeg", "-nostdin", "-loglevel", "error", "-i", str(path),
           "-vn", "-ac", "1", "-ar", "16000", "-f", "f32le", "pipe:1"]
    raw = subprocess.run(cmd, check=True, stdout=subprocess.PIPE).stdout
    return np.frombuffer(raw, dtype=np.float32)


def stamp(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def save_meta(meta_path: Path, meta: dict) -> None:
    meta_path.write_text(json.dumps(meta, indent=2))


def ask_llm(prompt: str, big: bool = False) -> str:
    body = json.dumps({
        "model": LLM, "prompt": prompt, "stream": False,
        "options": {"temperature": 0.2, "num_ctx": 12288 if big else 8192, "num_predict": 1800 if big else 900},
    }).encode()
    req = urllib.request.Request(OLLAMA, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=43200) as resp:
        return json.loads(resp.read())["response"].strip()


def chunks(text: str):
    lines, buf, size = text.splitlines(), [], 0
    for line in lines:
        if size + len(line) > CHUNK_CHARS and buf:
            yield "\n".join(buf)
            buf, size = [], 0
        buf.append(line)
        size += len(line) + 1
    if buf:
        yield "\n".join(buf)


def host_block(host_notes) -> str:
    items = [str(n).strip() for n in (host_notes or []) if str(n).strip()]
    if not items:
        return ""
    return ("CORRECTIONS FROM THE MEETING HOST (authoritative: they override the automatic transcript and the notes, "
            "use these names and facts in the summary):\n- " + "\n- ".join(items) + "\n\n")


def summarize(transcript: str, host_notes=None) -> str:
    if len(transcript.split()) < 15:
        return "## At a glance\nThis recording had too little speech to summarize.\n"
    hb = ("STYLE: always refer to people by first name only (for example Sam, not Sam Rivera), and never write \"referred to as\" or mention a name that was corrected. "
          "Mention everyone who attended, including people only named in the corrections.\n\n") + host_block(host_notes)
    notes = []
    for part in chunks(transcript):
        notes.append(ask_llm(hb + PROMPT_CHUNK + part))
    joined = "\n\n".join(notes)
    return ask_llm(hb + PROMPT_FINAL + joined, big=True) + "\n"


PROMPT_NAMES = (
    "Below is an automatic transcript of a meeting with speaker labels (Speaker 1, Speaker 2, ...). "
    "Work out which speakers can be named from the text itself. Only name a speaker when there is clear "
    "evidence: the speaker introduces themselves (for example, I am Sam, this is Sam), or another speaker "
    "addresses them by name and that speaker then answers. Do not guess. "
    "Reply with JSON only, in this exact shape: "
    '{"names": [{"speaker": "Speaker 1", "name": "First name", "evidence": "exact short quote from the transcript"}]}. '
    "Use an empty list if nobody can be named.\n\nTRANSCRIPT:\n"
)


def name_speakers(meta: dict, meta_path: Path, transcript_path: Path) -> None:
    text = transcript_path.read_text()
    speakers = sorted(set(re.findall(r"Speaker \d+", text)))
    if len(speakers) < 2 or len(text.split()) < 40:
        return
    sample = text if len(text) <= 24000 else text[:20000] + "\n...\n" + text[-4000:]
    raw = ask_llm(PROMPT_NAMES + sample)
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return
    data = json.loads(m.group(0))
    mapping = {}
    for item in data.get("names", []):
        spk = item.get("speaker", "")
        nm = str(item.get("name", "")).strip()
        ev = str(item.get("evidence", ""))
        if spk in speakers and re.fullmatch(r"[A-Za-z][A-Za-z. -]{0,30}", nm) and ev and ev.lower()[:40] in text.lower():
            mapping[spk] = nm
    if len(set(mapping.values())) != len(mapping):
        return
    if mapping:
        for spk, nm in mapping.items():
            text = text.replace(spk + ":", spk + " (" + nm + "):")
        transcript_path.write_text(text)
        meta["speaker_names"] = mapping
        save_meta(meta_path, meta)


def do_transcribe(meta: dict, meta_path: Path, model: WhisperModel) -> None:
    media = UPLOADS / meta["file"]
    meta["status"] = "transcribing"
    meta["transcribe_started_utc"] = now()
    save_meta(meta_path, meta)
    started = time.time()
    audio = load_audio(media)
    meta["audio_seconds"] = round(len(audio) / 16000, 1)
    segments, info = model.transcribe(audio, vad_filter=True, beam_size=1)
    segs = [(s.start, s.end, s.text.strip()) for s in segments]
    try:
        turns = diarize.diarize(audio)
        known = {}
        try:
            import voices
            known = voices.identify(voices.cluster_embeddings(audio, turns))
        except Exception:
            traceback.print_exc()
        labeled = diarize.assign(segs, turns, known)
        meta["speakers_found"] = len({lab for _t, _x, lab in labeled})
        meta["speakers_recognized"] = sorted(known.values())
    except Exception:
        traceback.print_exc()
        labeled = [(st, tx, None) for st, _en, tx in segs]
    lines = []
    prev = None
    for st, tx, lab in labeled:
        if lab and lab != prev:
            lines.append(f"[{stamp(st)}] {lab}: {tx}")
        else:
            lines.append(f"[{stamp(st)}] {tx}")
        prev = lab
    out = media.with_name(media.name + ".transcript.txt")
    out.write_text("\n".join(lines) + "\n")
    try:
        import names as _nm
        known = [x.get("name", "") for x in (meta.get("session") or {}).get("people", [])]
        fixed_text, _fx = _nm.fix_names(out.read_text(), known)
        if _fx:
            out.write_text(fixed_text)
    except Exception:
        traceback.print_exc()
    meta["language"] = info.language
    meta["transcript"] = out.name
    meta["transcribe_seconds"] = round(time.time() - started, 1)
    meta["status"] = "transcribed"
    save_meta(meta_path, meta)
    try:
        name_speakers(meta, meta_path, out)
    except Exception:
        traceback.print_exc()


def do_summarize(meta: dict, meta_path: Path) -> None:
    media = UPLOADS / meta["file"]
    meta["status"] = "summarizing"
    save_meta(meta_path, meta)
    started = time.time()
    text = (UPLOADS / meta["transcript"]).read_text()
    summary = summarize(text, meta.get("host_notes"))
    out = media.with_name(media.name + ".summary.md")
    out.write_text(summary)
    meta["summary"] = out.name
    meta["summary_model"] = LLM
    meta["summary_seconds"] = round(time.time() - started, 1)
    meta["status"] = "done"
    meta["done_utc"] = now()
    save_meta(meta_path, meta)


def send_mail(to_addr: str, subject: str, body: str) -> None:
    msg = EmailMessage()
    msg["From"] = MAIL_FROM
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=False)
    msg["Message-ID"] = make_msgid(domain="example.org")
    msg["Auto-Submitted"] = "auto-generated"
    msg.set_content(body)
    mailer.send_message(msg)


def do_publish(meta: dict, meta_path: Path) -> None:
    reviews = meta.get("reviews") or {}
    for addr in meta.get("recipients", []):
        reviews.setdefault(addr, secrets.token_urlsafe(32))
    meta["reviews"] = reviews
    save_meta(meta_path, meta)
    sent = meta.get("emailed_to") or []
    errors = []
    meta["email_attempts"] = meta.get("email_attempts", 0) + 1
    for addr, token in reviews.items():
        if addr in sent:
            continue
        try:
            notify.send_everyone_one(meta, addr, token, bool(meta.get("host_reviewed")))
            sent.append(addr)
        except Exception as exc:
            errors.append(addr + ": " + str(exc)[:120])
    meta["emailed_to"] = sent
    if errors:
        meta["email_errors"] = errors
    else:
        meta.pop("email_errors", None)
        meta["emailed_utc"] = now()
    save_meta(meta_path, meta)


def do_host_gate(meta: dict, meta_path: Path) -> None:
    """Host first: send the transcript to the host, then wait up to 6 hours for a corrected one."""
    if not meta.get("host_review_sent_utc"):
        meta["host_email_attempts"] = meta.get("host_email_attempts", 0) + 1
        try:
            notify.send_host_review(meta)
            meta["host_review_sent_utc"] = now()
        except Exception as exc:
            meta["host_email_error"] = str(exc)[:200]
            if meta["host_email_attempts"] >= 3:
                meta["host_gate_off"] = True
        save_meta(meta_path, meta)
        return
    if meta.get("host_revision_pending"):
        meta["host_revision_attempts"] = meta.get("host_revision_attempts", 0) + 1
        try:
            notify.send_host_review(meta, revised=True)
            meta["host_revision_pending"] = False
            meta["host_revision_sent_utc"] = now()
        except Exception as exc:
            meta["host_email_error"] = str(exc)[:200]
            if meta["host_revision_attempts"] >= 3:
                meta["host_revision_pending"] = False
        save_meta(meta_path, meta)
        return
    if meta.get("host_final_ready"):
        # the host sent their last changes and they are now in the summary: send the final email now
        meta["host_gate_off"] = True
        save_meta(meta_path, meta)
        return
    grace = (datetime.strptime(meta.get("review_deadline_utc", "9999-12-31T00:00:00Z"), "%Y-%m-%dT%H:%M:%SZ") + timedelta(minutes=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    if now() >= grace:
        meta["host_gate_off"] = True
        meta["host_review_expired"] = True
        save_meta(meta_path, meta)


def process(meta_path: Path, model_factory) -> None:
    meta = json.loads(meta_path.read_text())
    try:
        if meta["status"] == "uploaded":
            do_transcribe(meta, meta_path, model_factory())
        if meta["status"] == "transcribed":
            do_summarize(meta, meta_path)
        if (meta["status"] == "done" and meta.get("recipients") and not meta.get("emailed_utc")
                and meta.get("email_attempts", 0) < MAX_EMAIL_ATTEMPTS):
            gated = bool(meta.get("host")) and not meta.get("host_gate_off")
            if gated:
                do_host_gate(meta, meta_path)
            if not (bool(meta.get("host")) and not meta.get("host_gate_off")):
                do_publish(meta, meta_path)
    except Exception as exc:
        meta["status"] = "failed"
        meta["error"] = str(exc)[:500]
        meta["failed_utc"] = now()
        save_meta(meta_path, meta)
        traceback.print_exc()
    print(meta["file"] + ": " + meta["status"], flush=True)


def main() -> int:
    with open(LOCK, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("another run is active")
            return 0
        pending = []
        for p in sorted(UPLOADS.glob("*.json")):
            try:
                m = json.loads(p.read_text())
                # We hold the lock, so no worker is running. A job still marked as in progress
                # was killed (for example by the memory limit): put it back in the queue, up to 3 tries.
                if m.get("status") in ("transcribing", "summarizing"):
                    m["retries"] = m.get("retries", 0) + 1
                    if m["retries"] > 3:
                        m["status"] = "failed"
                        m["error"] = "Worker was killed 3 times (out of memory?)"
                        m["failed_utc"] = now()
                    else:
                        m["status"] = "uploaded" if m["status"] == "transcribing" else "transcribed"
                        print("requeued interrupted job " + m["file"] + " (try " + str(m["retries"]) + ")", flush=True)
                    save_meta(p, m)
                needs_mail = (m.get("status") == "done" and m.get("recipients")
                              and not m.get("emailed_utc")
                              and m.get("email_attempts", 0) < MAX_EMAIL_ATTEMPTS)
                if m.get("status") in ("uploaded", "transcribed") or needs_mail:
                    pending.append(p)
            except Exception:
                continue
        if not pending:
            return 0
        if quiet.meetings_live():
            print("a meeting is running or about to start; waiting")
            return 0
        holder = {}

        def model_factory():
            if "m" not in holder:
                holder["m"] = WhisperModel(WHISPER_SIZE, device="cpu", compute_type="int8",
                                           cpu_threads=5, download_root=MODELS)
            return holder["m"]

        for p in pending:
            process(p, model_factory)
    return 0


if __name__ == "__main__":
    sys.exit(main())
