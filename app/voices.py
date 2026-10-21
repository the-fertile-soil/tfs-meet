"""Saved voice signatures for TFS meetings. Stored only on this server, one file per person."""
import json
import os
from pathlib import Path

import numpy as np
import sherpa_onnx

import diarize

DIR = Path("/opt/tfs-review/voices")
THRESHOLD = 0.55
MARGIN = 0.08


def _extractor():
    cfg = sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(diarize.EMB), num_threads=2)
    return sherpa_onnx.SpeakerEmbeddingExtractor(cfg)


def _embed(ex, samples):
    s = ex.create_stream()
    s.accept_waveform(sample_rate=16000, waveform=samples.astype(np.float32))
    s.input_finished()
    v = np.array(ex.compute(s), dtype=np.float32)
    return v / (np.linalg.norm(v) + 1e-9)


def cluster_embeddings(audio, turns, max_seconds=90):
    """Return {speaker_index: unit embedding} using each speaker's longest turns."""
    ex = _extractor()
    by = {}
    for st, en, sp in turns:
        by.setdefault(sp, []).append((en - st, st, en))
    out = {}
    for sp, segs in by.items():
        segs.sort(reverse=True)
        parts, total = [], 0.0
        for d, st, en in segs:
            if d < 1.0:
                continue
            parts.append(audio[int(st * 16000):int(en * 16000)])
            total += d
            if total >= max_seconds:
                break
        if total >= 5.0:
            out[sp] = _embed(ex, np.concatenate(parts))
    return out


def load_all():
    people = {}
    if DIR.exists():
        for f in DIR.glob("*.json"):
            try:
                d = json.loads(f.read_text())
                people[d["name"]] = [np.array(e, dtype=np.float32) for e in d["embeddings"]]
            except Exception:
                continue
    return people


def enroll(name, emb, src, keep=5):
    DIR.mkdir(mode=0o700, exist_ok=True)
    f = DIR / (name.lower().replace(" ", "_") + ".json")
    embs = []
    if f.exists():
        embs = json.loads(f.read_text())["embeddings"]
    srcs = json.loads(f.read_text()).get("src", []) if f.exists() else []
    embs.append([float(x) for x in emb])
    srcs.append(src)
    f.write_text(json.dumps({"name": name, "embeddings": embs[-keep:], "src": srcs[-keep:]}))
    os.chmod(f, 0o600)


def identify(embs):
    """Return {speaker_index: name} for confident matches, each name used once."""
    people = load_all()
    if not people:
        return {}
    cand = []
    for sp, e in embs.items():
        scores = sorted(((max(float(e @ x) for x in v), n) for n, v in people.items()), reverse=True)
        best = scores[0]
        second = scores[1][0] if len(scores) > 1 else -1
        if best[0] >= THRESHOLD and best[0] - second >= MARGIN:
            cand.append((best[0], sp, best[1]))
    cand.sort(reverse=True)
    names, used = {}, set()
    for s, sp, n in cand:
        if n not in used:
            names[sp] = n
            used.add(n)
    return names


def forget_meeting(src):
    """Remove every signature that came from a deleted meeting; delete empty files."""
    if not DIR.exists():
        return
    for f in DIR.glob("*.json"):
        try:
            d = json.loads(f.read_text())
        except Exception:
            f.unlink()
            continue
        keep = [(e, x) for e, x in zip(d["embeddings"], d.get("src", [None] * len(d["embeddings"]))) if x not in (src, None)]
        if len(keep) == len(d["embeddings"]):
            continue
        if keep:
            d["embeddings"], d["src"] = [k[0] for k in keep], [k[1] for k in keep]
            f.write_text(json.dumps(d))
        else:
            f.unlink()
