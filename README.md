# tfs-meet

A self-hosted meeting service built on [Jitsi Meet](https://jitsi.org), made for small nonprofits.
It adds scheduling, automatic recording, transcripts, summaries and private review links on top of a standard Jitsi docker install, with no accounts for guests.

Built by [The Fertile Soil](https://thefertilesoil.org), shared so other organizations can run the same thing. See it in use at [thefertilesoil.org/meetings](https://thefertilesoil.org/meetings/).

## What it does

- Scheduled meetings with invitation emails. Everyone invited can start or join; no host has to be present.
- Walk-in meetings ("Now") and copyable invite links.
- Automatic recording with Jibri (several recorders side by side, each with its own ALSA loopback card).
- Per-room time limit with an on-screen warning, and a room size cap (Prosody modules).
- Transcription (faster-whisper) and speaker labels (sherpa-onnx diarization, optional saved voice signatures that are deleted with the meeting).
- Summaries written by a local model (Ollama), reviewed by the host before they go to everyone.
- Host can reply to the review email to correct names; spelling of known names is fixed in the transcript automatically.
- Private review links, video page, PDF and Word downloads, automatic deletion after a retention period.
- Attendance log (name, email, join and leave times) for a post-meeting report (see `prosody-modules/mod_tfs_state.lua` and `extras/mmn/`).

## Layout

| Path | What |
|---|---|
| `app/` | FastAPI app, background jobs, email templates (`/opt/tfs-review` on the server) |
| `prosody-modules/` | Jitsi Prosody modules: time limit, autorecord, room state and attendance log |
| `deploy/` | systemd units, docker compose override, nginx example, sample config files |
| `extras/mmn/` | A WordPress plugin and API for scheduling from a WordPress site, plus the post-meeting host report |

## Setup outline

1. Install Jitsi with docker-jitsi-meet (JWT auth, Jibri enabled). See `deploy/env.example` for the values this project expects.
2. Copy `prosody-modules/*.lua` into your Prosody custom plugins directory and list them in `XMPP_MUC_MODULES`.
3. Copy `app/` to `/opt/tfs-review`, create a venv, install `fastapi uvicorn faster-whisper sherpa-onnx python-docx reportlab`.
4. Create `jwt.json`, `mail.json`, `config.json` from the examples in `deploy/config/` (never commit the real ones).
5. Install the units in `deploy/systemd/`, put nginx in front (`deploy/nginx-vhost.conf.example`), and enable the timers.
6. Search the code for `example.org` and `Example Org` and replace them with your own domain and name. Branding lives mostly in `app/theme.py`, `app/notify.py`, `app/invite_tpl.html` and `app/static/`.

This is a working reference from a production install, not a polished package: paths are hard-coded to `/opt/tfs-review`, and some wording is specific to our use. Issues and improvements are welcome.

## Privacy

Recordings, transcripts and voice signatures are stored only for the retention period and are deleted together. Do not commit the `uploads/`, `voices/`, `schedules/` or `hosts/` directories or any `*.json` config containing secrets.

## License

Apache-2.0. See `LICENSE`. Jitsi, Prosody, faster-whisper and sherpa-onnx and their models carry their own licenses; check them before redistributing models.
