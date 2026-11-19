"""
Face recognition engines
========================

``FaceEngine`` is a small interface (detect + embed) so the recognition
backend can be swapped without touching the rest of the application.

* ``SFaceEngine``  - OpenCV YuNet detector + SFace recognizer (ONNX, CPU,
                     no GPU or dlib required). Default engine.
* ``NullEngine``   - used when models are missing or biometrics are disabled;
                     kiosks then switch automatically to PIN mode.
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)


class EngineUnavailable(RuntimeError):
    """The face engine cannot run (missing models, disabled, ...)."""


@dataclass
class Face:
    box: tuple                      # (x, y, w, h)
    score: float
    landmarks: np.ndarray | None = None   # 5 x 2 (eyes, nose, mouth corners)
    raw: object = field(default=None, repr=False)

    @property
    def size(self) -> int:
        return int(min(self.box[2], self.box[3]))

    @property
    def center(self) -> np.ndarray:
        x, y, w, h = self.box
        return np.array([x + w / 2.0, y + h / 2.0])


# ---------------------------------------------------------------------------
# Image decoding
# ---------------------------------------------------------------------------
def decode_image(data) -> np.ndarray:
    """Decode a data-URL / base64 string / raw bytes into a BGR image."""
    import cv2

    if isinstance(data, str):
        if "," in data and data.strip().startswith("data:"):
            data = data.split(",", 1)[1]
        data = base64.b64decode(data)
    buf = np.frombuffer(data, dtype=np.uint8)
    img = cv2.imdecode(buf, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("Invalid image")
    return img


def normalize(vec: np.ndarray) -> np.ndarray:
    vec = np.asarray(vec, dtype=np.float32).ravel()
    n = float(np.linalg.norm(vec))
    return vec / n if n > 0 else vec


# ---------------------------------------------------------------------------
# Engine interface
# ---------------------------------------------------------------------------
class FaceEngine:
    name = "base"
    available = True

    def detect(self, img: np.ndarray) -> list[Face]:
        raise NotImplementedError

    def embed(self, img: np.ndarray, face: Face) -> np.ndarray:
        """Return an L2-normalised embedding (float32 vector)."""
        raise NotImplementedError


class NullEngine(FaceEngine):
    name = "none"
    available = False

    def __init__(self, reason="Face recognition disabled"):
        self.reason = reason

    def detect(self, img):
        raise EngineUnavailable(self.reason)

    def embed(self, img, face):
        raise EngineUnavailable(self.reason)


# ---------------------------------------------------------------------------
# OpenCV YuNet + SFace
# ---------------------------------------------------------------------------
class SFaceEngine(FaceEngine):
    name = "sface"

    def __init__(self, models_dir, detector_file, recognizer_file,
                 score_threshold=0.85):
        import cv2

        det_path = Path(models_dir) / detector_file
        rec_path = Path(models_dir) / recognizer_file
        missing = [str(p) for p in (det_path, rec_path) if not p.exists()]
        if missing:
            raise EngineUnavailable(
                "Missing model files: " + ", ".join(missing)
                + " (run: python scripts/download_models.py)")
        self._cv2 = cv2
        self._detector = cv2.FaceDetectorYN.create(
            str(det_path), "", (320, 320), score_threshold, 0.3, 50)
        self._recognizer = cv2.FaceRecognizerSF.create(str(rec_path), "")

    def detect(self, img):
        h, w = img.shape[:2]
        self._detector.setInputSize((w, h))
        _, faces = self._detector.detect(img)
        result = []
        if faces is None:
            return result
        for row in faces:
            x, y, fw, fh = (int(v) for v in row[:4])
            result.append(Face(box=(x, y, fw, fh), score=float(row[14]),
                               landmarks=row[4:14].reshape(5, 2).copy(), raw=row))
        result.sort(key=lambda f: f.box[2] * f.box[3], reverse=True)
        return result

    def embed(self, img, face):
        aligned = self._recognizer.alignCrop(img, face.raw)
        feature = self._recognizer.feature(aligned)
        return normalize(feature)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------
_REGISTRY = {}


def register_engine(name, factory):
    """Register an extra engine (used by tests, or for a custom backend)."""
    _REGISTRY[name] = factory


def create_engine(config) -> FaceEngine:
    if not config.get("BIOMETRICS_ENABLED", True):
        return NullEngine("Biometrics disabled by configuration")
    name = config.get("FACE_ENGINE", "sface")
    try:
        if name in _REGISTRY:
            return _REGISTRY[name](config)
        if name == "sface":
            return SFaceEngine(config["MODELS_DIR"], config["DETECTOR_MODEL"],
                               config["RECOGNIZER_MODEL"])
        raise EngineUnavailable(f"Unknown face engine '{name}'")
    except EngineUnavailable as exc:
        log.warning("Face engine unavailable, kiosks will use PIN mode: %s", exc)
        return NullEngine(str(exc))
    except Exception as exc:  # pragma: no cover - defensive (corrupt model, ...)
        log.exception("Face engine failed to start")
        return NullEngine(f"Face engine failed to start: {exc}")
