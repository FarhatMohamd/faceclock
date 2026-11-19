"""
Test fixtures
=============

A deterministic ``FakeEngine`` replaces the neural network: the "identity" of
a test image is its colour, so a solid red picture always matches the employee
enrolled with red pictures.
"""

import base64

import cv2
import numpy as np
import pytest

from faceclock import create_app
from faceclock.cli import create_kiosk, ensure_defaults
from faceclock.config import TestConfig
from faceclock.db import execute, now_iso
from faceclock.face.engine import Face, FaceEngine, normalize, register_engine
from faceclock.security import hash_password, hash_pin

# Colours (BGR) used as fake "faces"
RED, GREEN, BLUE, WHITE = (0, 0, 255), (0, 255, 0), (255, 0, 0), (255, 255, 255)


class FakeEngine(FaceEngine):
    name = "fake"

    def detect(self, img):
        if float(img.mean()) < 5:                     # black image => nobody
            return []
        h, w = img.shape[:2]
        return [Face(box=(0, 0, w, h), score=0.99)]

    def embed(self, img, face):
        mean = img.reshape(-1, 3).mean(axis=0) - 128.0
        return normalize(mean)


register_engine("fake", lambda cfg: FakeEngine())


def make_image(color, size=160):
    img = np.zeros((size, size, 3), dtype=np.uint8)
    img[:] = color
    ok, buf = cv2.imencode(".png", img)
    return "data:image/png;base64," + base64.b64encode(buf.tobytes()).decode()


@pytest.fixture
def app(tmp_path):
    app = create_app(TestConfig, DATABASE=str(tmp_path / "test.db"))
    with app.app_context():
        ensure_defaults()
        seed(app)
    return app


def seed(app):
    """Two buildings, two directions, three employees, two kiosks, users."""
    d = app.config.setdefault("SEED", {})
    d["site_a"] = execute("INSERT INTO sites (name, timezone) VALUES ('Building A', 'Europe/Paris')")
    d["site_b"] = execute("INSERT INTO sites (name, timezone) VALUES ('Building B', 'Europe/Paris')")
    d["dir_ops"] = execute("INSERT INTO departments (name, code) VALUES ('Operations', 'OPS')")
    d["svc_log"] = execute("INSERT INTO departments (name, code, parent_id) VALUES "
                           "('Logistics', 'OPS-LOG', ?)", (d["dir_ops"],))
    d["dir_fin"] = execute("INSERT INTO departments (name, code) VALUES ('Finance', 'FIN')")
    d["schedule"] = execute("INSERT INTO schedules (name, start_time, end_time, workdays, "
                            "grace_minutes, expected_minutes) VALUES "
                            "('Day', '09:00', '17:00', '1,2,3,4,5,6,7', 5, 420)")

    def employee(code, first, last, dep, pin, color=None):
        emp_id = execute(
            "INSERT INTO employees (employee_code, first_name, last_name, department_id, "
            "home_site_id, schedule_id, pin_hash, biometric_consent, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (code, first, last, dep, d["site_a"], d["schedule"], hash_pin(pin),
             1 if color else 0, now_iso()))
        if color:
            emb = normalize(np.array(color, dtype=np.float32) - 128.0)
            execute("INSERT INTO face_templates (employee_id, engine, embedding, created_at) "
                    "VALUES (?, 'fake', ?, ?)", (emp_id, emb.tobytes(), now_iso()))
        return emp_id

    d["alice"] = employee("E1", "Alice", "Martin", d["svc_log"], "4821", RED)
    d["bob"] = employee("E2", "Bob", "Durand", d["dir_fin"], "7395", GREEN)
    d["carl"] = employee("E3", "Carl", "Petit", d["dir_ops"], "5162")   # no biometrics

    d["kiosk_a_id"], d["kiosk_a"] = create_kiosk("Entrance A", d["site_a"])
    d["kiosk_b_id"], d["kiosk_b"] = create_kiosk("Entrance B", d["site_b"])

    execute("INSERT INTO users (username, password_hash, role) VALUES ('admin', ?, 'ADMIN')",
            (hash_password("admin-pass"),))
    execute("INSERT INTO users (username, password_hash, role, department_id) VALUES "
            "('ops', ?, 'MANAGER', ?)", (hash_password("ops-pass"), d["dir_ops"]))


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def seed_data(app):
    return app.config["SEED"]


@pytest.fixture
def kiosk(client, seed_data):
    """Small helper to call the kiosk API."""

    class K:
        def __init__(self, token):
            self.token = token

        def post(self, path, json=None):
            return client.post(f"/api/kiosk{path}", json=json or {},
                               headers={"X-Kiosk-Token": self.token})

        def get(self, path):
            return client.get(f"/api/kiosk{path}", headers={"X-Kiosk-Token": self.token})

        def identify(self, color):
            return self.post("/identify", {"frames": [make_image(color)]})

        def punch_face(self, color, action=None, break_type=None):
            r = self.identify(color).get_json()
            assert r["ok"], r
            return self.post("/confirm", {"ticket": r["ticket"],
                                          "action": action or r["suggested_action"],
                                          "break_type": break_type})

    return {"A": K(seed_data["kiosk_a"]), "B": K(seed_data["kiosk_b"])}


def login(client, username, password):
    return client.post("/admin/login", data={"username": username, "password": password})
