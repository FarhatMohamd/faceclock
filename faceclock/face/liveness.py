"""
Basic liveness check (anti photo / anti replay)
===============================================

The kiosk sends a short burst of frames (~3 frames, 250 ms apart). A live
person always produces small natural movements; a still photo or a replayed
identical image does not.

This heuristic stops casual fraud (holding a printed photo still, re-sending
the same image). It is NOT a certified presentation-attack detector: for high
security sites, plug a dedicated anti-spoofing model into ``check_liveness``.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class LivenessResult:
    ok: bool
    reason: str = ""
    motion: float = 0.0


MIN_MOTION = 0.004    # relative landmark motion below this => static image
MAX_MOTION = 0.35     # above this => frames from different scenes / persons
MIN_CONSISTENCY = 0.55


def check_liveness(frames, faces, embeddings) -> LivenessResult:
    """``frames``: images, ``faces``: one Face per frame, ``embeddings``: one per frame."""
    if len(frames) < 2:
        return LivenessResult(False, "NEED_MULTIPLE_FRAMES")

    # 1. Identical frames => replayed picture
    diffs = [float(np.mean(np.abs(frames[i].astype(np.int16) - frames[i - 1].astype(np.int16))))
             for i in range(1, len(frames)) if frames[i].shape == frames[i - 1].shape]
    if diffs and max(diffs) < 0.5:
        return LivenessResult(False, "STATIC_IMAGE")

    # 2. Same identity on every frame
    for i in range(1, len(embeddings)):
        if float(np.dot(embeddings[0], embeddings[i])) < MIN_CONSISTENCY:
            return LivenessResult(False, "INCONSISTENT_FRAMES")

    # 3. Natural micro-movements of facial landmarks
    motions = []
    for i in range(1, len(faces)):
        a, b = faces[i - 1], faces[i]
        scale = max(1.0, float(a.size))
        if a.landmarks is not None and b.landmarks is not None:
            d = np.linalg.norm(b.landmarks - a.landmarks, axis=1).mean()
        else:
            d = np.linalg.norm(b.center - a.center)
        motions.append(d / scale)
    motion = float(np.mean(motions)) if motions else 0.0
    if motion < MIN_MOTION:
        return LivenessResult(False, "NO_MOTION", motion)
    if motion > MAX_MOTION:
        return LivenessResult(False, "TOO_MUCH_MOTION", motion)
    return LivenessResult(True, "", motion)
