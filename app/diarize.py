"""Speaker separation for TFS meetings (local, CPU, no outside services)."""
from pathlib import Path

import numpy as np
import sherpa_onnx

BASE = Path("/opt/tfs-review/diar")
SEG = BASE / "sherpa-onnx-pyannote-segmentation-3-0" / "model.onnx"
EMB = BASE / "emb.onnx"


def diarize(audio: np.ndarray, threads: int = 4):
    """Return a list of (start, end, speaker_index) from 16 kHz mono float32 audio."""
    config = sherpa_onnx.OfflineSpeakerDiarizationConfig(
        segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
            pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(model=str(SEG)),
            num_threads=threads,
        ),
        embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(EMB), num_threads=threads),
        clustering=sherpa_onnx.FastClusteringConfig(num_clusters=-1, threshold=1.2),
        min_duration_on=0.3,
        min_duration_off=0.5,
    )
    sd = sherpa_onnx.OfflineSpeakerDiarization(config)
    result = sd.process(audio).sort_by_start_time()
    return [(r.start, r.end, r.speaker) for r in result]


def assign(segments, turns, names=None):
    """Give each transcript segment (start, end, text) the speaker with the most overlap.

    Returns a list of (start, text, label) where label is Speaker 1, 2, ... numbered
    in order of first appearance.
    """
    order = {}
    out = []
    last = None
    for start, end, text in segments:
        best, best_ov = None, 0.0
        for ts, te, sp in turns:
            ov = min(end, te) - max(start, ts)
            if ov > best_ov:
                best, best_ov = sp, ov
        if best is None:
            best = last
        if best is None:
            best = -1
        last = best
        if best not in order:
            order[best] = len(order) + 1
        out.append((start, text, (names or {}).get(best) or "Speaker " + str(order[best])))
    return out
