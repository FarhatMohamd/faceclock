"""
Configuration
=============

Every setting can be overridden with an environment variable of the same
name (prefixed with ``FACECLOCK_``), e.g. ``FACECLOCK_MATCH_THRESHOLD=0.40``.
"""

import os
import secrets
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _env(name, default, cast=str):
    """Read ``FACECLOCK_<name>`` from the environment, with type casting."""
    raw = os.environ.get(f"FACECLOCK_{name}")
    if raw is None:
        return default
    if cast is bool:
        return raw.strip().lower() in ("1", "true", "yes", "on")
    return cast(raw)


class Config:
    # --- Core -----------------------------------------------------------
    SECRET_KEY = _env("SECRET_KEY", None) or secrets.token_hex(32)
    DATABASE = _env("DATABASE", str(BASE_DIR / "instance" / "faceclock.db"))
    MAX_CONTENT_LENGTH = 8 * 1024 * 1024          # max upload (3 webcam frames)
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = _env("SECURE_COOKIES", False, bool)
    CSRF_ENABLED = True

    # --- Face recognition -------------------------------------------------
    # Global switch: when False the system works as a PIN-only time clock
    # (useful where biometric time tracking is not permitted).
    BIOMETRICS_ENABLED = _env("BIOMETRICS_ENABLED", True, bool)
    FACE_ENGINE = _env("FACE_ENGINE", "sface")
    MODELS_DIR = _env("MODELS_DIR", str(BASE_DIR / "models"))
    DETECTOR_MODEL = "face_detection_yunet_2023mar.onnx"
    RECOGNIZER_MODEL = "face_recognition_sface_2021dec.onnx"
    MATCH_THRESHOLD = _env("MATCH_THRESHOLD", 0.42, float)   # cosine similarity
    MATCH_MARGIN = _env("MATCH_MARGIN", 0.06, float)         # best vs 2nd best
    MIN_FACE_SIZE = _env("MIN_FACE_SIZE", 80, int)           # pixels
    LIVENESS_ENABLED = _env("LIVENESS_ENABLED", True, bool)
    ENROLL_MIN_SAMPLES = 3

    # --- Punch rules ------------------------------------------------------
    TICKET_TTL_SECONDS = 30            # time allowed to confirm a recognition
    DEBOUNCE_SECONDS = 60              # ignore an identical punch repeated quickly
    MAX_SHIFT_HOURS = 16               # an open shift older than this is "forgotten"
    OFFLINE_MAX_AGE_HOURS = 72         # offline punches older than this are refused
    OFFLINE_REQUIRES_REVIEW = True     # offline punches go to the review queue
    PIN_REQUIRES_REVIEW = False        # set True to review every PIN punch

    # --- PIN security -----------------------------------------------------
    PIN_MIN_LENGTH = 4
    PIN_MAX_ATTEMPTS = 5
    PIN_LOCK_MINUTES = 15

    # --- GDPR -------------------------------------------------------------
    PUNCH_RETENTION_DAYS = _env("PUNCH_RETENTION_DAYS", 5 * 365, int)
    INCIDENT_RETENTION_DAYS = 180


class TestConfig(Config):
    TESTING = True
    SECRET_KEY = "test-secret"
    FACE_ENGINE = "fake"
    LIVENESS_ENABLED = False
    CSRF_ENABLED = False
