"""
Face gallery and 1:N identification
===================================

Templates are loaded from the database into a NumPy matrix and matched with
cosine similarity. The cache is refreshed automatically whenever templates are
added or removed (works with several worker processes).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

import numpy as np

from ..db import query


@dataclass
class Match:
    employee_id: int | None
    score: float
    second_score: float
    accepted: bool
    reason: str = ""


class FaceGallery:
    def __init__(self):
        self._lock = threading.Lock()
        self._signature = None
        self._matrix = np.zeros((0, 0), dtype=np.float32)
        self._owners = np.zeros((0,), dtype=np.int64)

    # -- Cache ---------------------------------------------------------------
    def _current_signature(self, engine_name):
        row = query("SELECT COUNT(*) AS n, COALESCE(MAX(id), 0) AS m, "
                    "COALESCE(SUM(id), 0) AS s FROM face_templates WHERE engine = ?",
                    (engine_name,), one=True)
        return (engine_name, row["n"], row["m"], row["s"])

    def refresh(self, engine_name):
        sig = self._current_signature(engine_name)
        if sig == self._signature:
            return
        with self._lock:
            rows = query(
                "SELECT t.employee_id, t.embedding FROM face_templates t "
                "JOIN employees e ON e.id = t.employee_id "
                "WHERE t.engine = ? AND e.active = 1 AND e.biometric_consent = 1",
                (engine_name,))
            if rows:
                self._matrix = np.vstack(
                    [np.frombuffer(r["embedding"], dtype=np.float32) for r in rows])
                self._owners = np.array([r["employee_id"] for r in rows], dtype=np.int64)
            else:
                self._matrix = np.zeros((0, 0), dtype=np.float32)
                self._owners = np.zeros((0,), dtype=np.int64)
            self._signature = sig

    # -- Matching ------------------------------------------------------------
    def identify(self, embedding, engine_name, threshold, margin, allowed_ids=None) -> Match:
        """Find the best matching employee.

        A match is accepted only if its score is above ``threshold`` AND it is
        clearly better than the best *other* employee (``margin``), which
        avoids confusing look-alikes / twins.
        """
        self.refresh(engine_name)
        if self._matrix.size == 0:
            return Match(None, 0.0, 0.0, False, "EMPTY_GALLERY")

        scores = self._matrix @ np.asarray(embedding, dtype=np.float32)
        owners = self._owners
        if allowed_ids is not None:
            mask = np.isin(owners, np.fromiter(allowed_ids, dtype=np.int64))
            scores, owners = scores[mask], owners[mask]
            if scores.size == 0:
                return Match(None, 0.0, 0.0, False, "NO_CANDIDATE")

        # Best score per employee (an employee may have several templates)
        best = {}
        for emp_id, s in zip(owners.tolist(), scores.tolist()):
            if s > best.get(emp_id, -1.0):
                best[emp_id] = s
        ranked = sorted(best.items(), key=lambda kv: kv[1], reverse=True)
        top_id, top = ranked[0]
        second = ranked[1][1] if len(ranked) > 1 else -1.0

        if top < threshold:
            return Match(None, top, second, False, "LOW_SCORE")
        if second > -1.0 and (top - second) < margin:
            return Match(top_id, top, second, False, "AMBIGUOUS")
        return Match(top_id, top, second, True)

    def nearest_other(self, embedding, engine_name, exclude_id):
        """Highest similarity with any employee other than ``exclude_id``
        (used at enrolment to detect duplicate identities)."""
        self.refresh(engine_name)
        if self._matrix.size == 0:
            return None, 0.0
        scores = self._matrix @ np.asarray(embedding, dtype=np.float32)
        mask = self._owners != exclude_id
        if not mask.any():
            return None, 0.0
        idx = int(np.argmax(np.where(mask, scores, -2.0)))
        return int(self._owners[idx]), float(scores[idx])
