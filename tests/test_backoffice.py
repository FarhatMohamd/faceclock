"""Back-office, employee portal, enrolment, audit log and liveness."""

import numpy as np
from conftest import BLUE, RED, login, make_image

from faceclock.db import execute, query
from faceclock.face.engine import Face
from faceclock.face.liveness import check_liveness
from faceclock.services.audit import verify_chain


# ---------------------------------------------------------------------------
# Back-office access control
# ---------------------------------------------------------------------------
def test_pages_require_login(client):
    assert client.get("/admin/").status_code == 302


def test_admin_pages_render(client, kiosk):
    kiosk["A"].punch_face(RED, "IN")
    assert login(client, "admin", "admin-pass").status_code == 302
    for url in ("/admin/", "/admin/presence", "/admin/employees", "/admin/organization",
                "/admin/timesheets", "/admin/review", "/admin/audit", "/admin/employees/new"):
        r = client.get(url)
        assert r.status_code == 200, url
    assert b"Alice" in client.get("/admin/presence").data


def test_csv_export(client, kiosk):
    kiosk["A"].punch_face(RED, "IN")
    login(client, "admin", "admin-pass")
    r = client.get("/admin/timesheets/export.csv")
    assert r.status_code == 200 and r.mimetype == "text/csv"
    assert "employee_code;last_name" in r.data.decode("utf-8-sig")


def test_manager_sees_only_his_direction(client, seed_data):
    login(client, "ops", "ops-pass")
    page = client.get("/admin/employees").data
    assert b"Alice" in page and b"Carl" in page          # Operations + sub-service Logistics
    assert b"Bob" not in page                            # Finance
    assert client.get(f"/admin/employees/{seed_data['bob']}").status_code == 403
    assert client.get("/admin/organization").status_code == 403


# ---------------------------------------------------------------------------
# Enrolment
# ---------------------------------------------------------------------------
def test_enrolment_requires_consent(client, seed_data):
    login(client, "admin", "admin-pass")
    r = client.post(f"/admin/employees/{seed_data['carl']}/enroll",
                    json={"frames": [make_image(BLUE)] * 3})
    assert r.get_json()["error"] == "NO_CONSENT"


def test_enrolment_and_duplicate_detection(client, app, seed_data, kiosk):
    with app.app_context():
        execute("UPDATE employees SET biometric_consent = 1 WHERE id = ?", (seed_data["carl"],))
    login(client, "admin", "admin-pass")
    dup = client.post(f"/admin/employees/{seed_data['carl']}/enroll",
                      json={"frames": [make_image(RED)] * 3})        # Alice's face
    assert dup.get_json()["error"] == "DUPLICATE_IDENTITY"
    ok = client.post(f"/admin/employees/{seed_data['carl']}/enroll",
                     json={"frames": [make_image(BLUE)] * 3})
    assert ok.get_json() == {"ok": True, "samples": 3}
    assert kiosk["A"].identify(BLUE).get_json()["employee"]["name"] == "Carl P."


# ---------------------------------------------------------------------------
# Employee portal and correction workflow
# ---------------------------------------------------------------------------
def test_correction_request_approved_by_manager(app, client, seed_data):
    r = client.post("/me/login", data={"employee_code": "E1", "pin": "4821"})
    assert r.status_code == 302
    assert client.get("/me/").status_code == 200
    client.post("/me/corrections", data={"action": "ADD", "punch_type": "IN",
                                         "ts": "2026-03-10T09:00", "reason": "Forgot to clock in"})
    client.get("/me/logout")

    login(client, "ops", "ops-pass")
    with app.app_context():
        req = query("SELECT * FROM correction_requests", one=True)
    assert req["status"] == "PENDING"
    client.post(f"/admin/corrections/{req['id']}/review", data={"decision": "approve"})
    with app.app_context():
        p = query("SELECT * FROM punches WHERE employee_id = ?", (seed_data["alice"],), one=True)
        req = query("SELECT * FROM correction_requests", one=True)
    assert req["status"] == "APPROVED"
    assert p["method"] == "ADMIN" and p["ts"] == "2026-03-10T08:00:00Z"   # 09:00 Paris (UTC+1)


def test_employee_can_withdraw_consent(app, client, seed_data, kiosk):
    client.post("/me/login", data={"employee_code": "E1", "pin": "4821"})
    client.post("/me/consent/withdraw")
    with app.app_context():
        n = query("SELECT COUNT(*) n FROM face_templates WHERE employee_id = ?",
                  (seed_data["alice"],), one=True)["n"]
    assert n == 0
    assert kiosk["A"].identify(RED).get_json()["error"] == "NOT_RECOGNIZED"


# ---------------------------------------------------------------------------
# Audit log integrity
# ---------------------------------------------------------------------------
def test_audit_chain_detects_tampering(app, kiosk):
    kiosk["A"].punch_face(RED, "IN")
    with app.app_context():
        assert verify_chain() == (True, None)
        execute("UPDATE audit_log SET actor = 'someone-else' WHERE id = 2")
        ok, broken = verify_chain()
    assert not ok and broken == 2


# ---------------------------------------------------------------------------
# Liveness heuristic
# ---------------------------------------------------------------------------
def _face(dx=0.0):
    lm = np.array([[40, 50], [80, 50], [60, 70], [45, 90], [75, 90]], dtype=np.float32) + dx
    return Face(box=(10, 10, 100, 100), score=0.99, landmarks=lm)


def test_liveness_rejects_static_image():
    img = np.full((120, 120, 3), 100, np.uint8)
    emb = np.ones(4, np.float32) / 2
    res = check_liveness([img, img.copy(), img.copy()], [_face()] * 3, [emb] * 3)
    assert not res.ok and res.reason == "STATIC_IMAGE"


def test_liveness_accepts_natural_motion():
    rng = np.random.default_rng(0)
    frames = [rng.integers(0, 255, (120, 120, 3), dtype=np.uint8) for _ in range(3)]
    emb = np.ones(4, np.float32) / 2
    res = check_liveness(frames, [_face(0), _face(1.5), _face(3.0)], [emb] * 3)
    assert res.ok, res


def test_health_endpoint(client):
    assert client.get("/health").get_json()["status"] == "ok"
