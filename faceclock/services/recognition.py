"""
Recognition service: enrolment and identification
=================================================
"""

from __future__ import annotations

import numpy as np
from flask import current_app

from ..db import execute, now_iso, query
from ..face import EngineUnavailable, decode_image, normalize
from ..face.liveness import check_liveness
from .audit import audit


class RecognitionError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def engine():
    return current_app.extensions["faceclock"]["engine"]


def gallery():
    return current_app.extensions["faceclock"]["gallery"]


def biometrics_available() -> bool:
    return bool(getattr(engine(), "available", False))


# ---------------------------------------------------------------------------
# Frame analysis
# ---------------------------------------------------------------------------
def _analyse(images):
    """Decode frames, detect exactly one face per frame, compute embeddings."""
    eng = engine()
    min_size = current_app.config["MIN_FACE_SIZE"]
    frames, faces, embeddings = [], [], []
    for data in images:
        try:
            img = decode_image(data)
        except Exception:
            raise RecognitionError("BAD_IMAGE", "Invalid image received")
        try:
            detected = eng.detect(img)
        except EngineUnavailable as exc:
            raise RecognitionError("ENGINE_UNAVAILABLE", str(exc))
        if not detected:
            raise RecognitionError("NO_FACE", "No face detected, please face the camera")
        if len(detected) > 1 and detected[1].size >= 0.6 * detected[0].size:
            raise RecognitionError("MULTIPLE_FACES", "One person at a time, please")
        face = detected[0]
        if face.size < min_size:
            raise RecognitionError("FACE_TOO_SMALL", "Please come closer to the camera")
        frames.append(img)
        faces.append(face)
        embeddings.append(eng.embed(img, face))
    return frames, faces, embeddings


def allowed_employee_ids_for_site(site_id):
    """Employees allowed at a site: unrestricted ones + those explicitly listed."""
    rows = query(
        "SELECT e.id FROM employees e WHERE e.active = 1 AND e.biometric_consent = 1 AND ("
        "  NOT EXISTS (SELECT 1 FROM employee_sites es WHERE es.employee_id = e.id) "
        "  OR EXISTS (SELECT 1 FROM employee_sites es WHERE es.employee_id = e.id "
        "             AND es.site_id = ?))", (site_id,))
    return {r["id"] for r in rows}


def identify(images, site_id):
    """Identify the person in front of a kiosk. Returns (employee_row, match)."""
    cfg = current_app.config
    if not images:
        raise RecognitionError("BAD_IMAGE", "No image received")
    frames, faces, embeddings = _analyse(images[:5])

    if cfg["LIVENESS_ENABLED"]:
        live = check_liveness(frames, faces, embeddings)
        if not live.ok:
            raise RecognitionError("LIVENESS_FAILED",
                                   "Liveness check failed, please look at the camera and retry")

    probe = normalize(np.mean(np.vstack(embeddings), axis=0))
    match = gallery().identify(probe, engine().name, cfg["MATCH_THRESHOLD"], cfg["MATCH_MARGIN"],
                               allowed_ids=allowed_employee_ids_for_site(site_id))
    if not match.accepted:
        code = "AMBIGUOUS" if match.reason == "AMBIGUOUS" else "NOT_RECOGNIZED"
        raise RecognitionError(code, "Face not recognised, use your employee code and PIN")
    emp = query("SELECT * FROM employees WHERE id = ?", (match.employee_id,), one=True)
    return emp, match


# ---------------------------------------------------------------------------
# Enrolment
# ---------------------------------------------------------------------------
def enroll(employee, images, actor):
    """Create face templates from several photos (only embeddings are kept)."""
    cfg = current_app.config
    if not employee["biometric_consent"]:
        raise RecognitionError("NO_CONSENT", "The employee has not given biometric consent")
    if len(images) < cfg["ENROLL_MIN_SAMPLES"]:
        raise RecognitionError("NOT_ENOUGH_SAMPLES",
                               f"At least {cfg['ENROLL_MIN_SAMPLES']} photos are required")

    _, faces, embeddings = _analyse(images)

    # Samples must be consistent (same person on every photo)
    mean = normalize(np.mean(np.vstack(embeddings), axis=0))
    if min(float(np.dot(mean, e)) for e in embeddings) < 0.5:
        raise RecognitionError("INCONSISTENT_SAMPLES", "Photos do not show the same person")

    # Guard against enrolling a face that already belongs to another employee
    other_id, other_score = gallery().nearest_other(mean, engine().name, employee["id"])
    if other_id is not None and other_score >= cfg["MATCH_THRESHOLD"] + 0.08:
        raise RecognitionError("DUPLICATE_IDENTITY",
                               "This face is already enrolled for another employee")

    ts = now_iso()
    for face, emb in zip(faces, embeddings):
        execute("INSERT INTO face_templates (employee_id, engine, embedding, quality, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (employee["id"], engine().name, np.asarray(emb, np.float32).tobytes(),
                 face.score, ts))
    audit(actor, "FACE_ENROLLED", "employee", employee["id"], {"samples": len(embeddings)})
    return len(embeddings)


def delete_biometrics(employee_id, actor):
    execute("DELETE FROM face_templates WHERE employee_id = ?", (employee_id,))
    audit(actor, "FACE_DELETED", "employee", employee_id)
