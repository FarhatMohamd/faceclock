"""End-to-end kiosk scenarios: face punch, confirmation key, PIN fallback, offline mode."""

from datetime import timedelta

from conftest import BLUE, GREEN, RED, WHITE, make_image

from faceclock.db import query, to_iso, utcnow


# ---------------------------------------------------------------------------
# Face recognition + confirmation
# ---------------------------------------------------------------------------
def test_unauthenticated_kiosk_is_refused(client):
    r = client.post("/api/kiosk/identify", json={})
    assert r.status_code == 401


def test_kiosk_config(kiosk):
    cfg = kiosk["A"].get("/config").get_json()
    assert cfg["biometrics"] is True
    assert cfg["site"]["name"] == "Building A"
    assert {b["code"] for b in cfg["break_types"]} >= {"SMOKE", "COFFEE", "LUNCH"}


def test_identify_returns_ticket_and_suggestion(kiosk):
    r = kiosk["A"].identify(RED).get_json()
    assert r["ok"]
    assert r["employee"]["name"] == "Alice M."          # privacy: initial only
    assert r["state"] == "OFF"
    assert r["suggested_action"] == "IN"
    assert r["allowed_actions"] == ["IN"]


def test_full_day_with_smoking_break(kiosk, app, seed_data):
    k = kiosk["A"]
    assert k.punch_face(RED, "IN").get_json()["ok"]

    r = k.identify(RED).get_json()
    assert r["state"] == "WORKING" and "BREAK_START" in r["allowed_actions"]
    assert k.post("/confirm", {"ticket": r["ticket"], "action": "BREAK_START",
                               "break_type": "SMOKE"}).get_json()["ok"]

    r = k.identify(RED).get_json()
    assert r["state"] == "ON_BREAK" and r["on_break_type"] == "SMOKE"
    assert r["suggested_action"] == "BREAK_END"
    assert k.post("/confirm", {"ticket": r["ticket"], "action": "BREAK_END"}).get_json()["ok"]

    assert k.punch_face(RED, "OUT").get_json()["ok"]
    with app.app_context():
        types = [p["punch_type"] for p in query(
            "SELECT punch_type FROM punches WHERE employee_id = ? ORDER BY id", (seed_data["alice"],))]
    assert types == ["IN", "BREAK_START", "BREAK_END", "OUT"]


def test_invalid_transition_is_refused(kiosk):
    r = kiosk["A"].identify(RED).get_json()
    res = kiosk["A"].post("/confirm", {"ticket": r["ticket"], "action": "OUT"})
    assert res.status_code == 409
    assert res.get_json()["error"] == "INVALID_TRANSITION"


def test_ticket_cannot_be_reused(kiosk):
    r = kiosk["A"].identify(RED).get_json()
    assert kiosk["A"].post("/confirm", {"ticket": r["ticket"], "action": "IN"}).get_json()["ok"]
    again = kiosk["A"].post("/confirm", {"ticket": r["ticket"], "action": "IN"})
    assert again.get_json()["error"] == "TICKET_USED"


def test_ticket_bound_to_kiosk(kiosk):
    r = kiosk["A"].identify(RED).get_json()
    res = kiosk["B"].post("/confirm", {"ticket": r["ticket"], "action": "IN"})
    assert res.get_json()["error"] == "BAD_TICKET"


def test_unknown_face_and_no_face(kiosk):
    assert kiosk["A"].identify(WHITE).get_json()["error"] == "NOT_RECOGNIZED"
    assert kiosk["A"].identify((0, 0, 0)).get_json()["error"] == "NO_FACE"


def test_employee_without_consent_is_never_recognised(kiosk):
    # Carl has no template; a blue face must not be attributed to anyone.
    assert kiosk["A"].identify(BLUE).get_json()["error"] == "NOT_RECOGNIZED"


# ---------------------------------------------------------------------------
# "Not me" correction + secret PIN fallback
# ---------------------------------------------------------------------------
def test_not_me_then_pin_correction(kiosk, app, seed_data):
    k = kiosk["A"]
    wrong = k.identify(RED).get_json()             # Bob stands in front, Alice is proposed
    assert k.post("/reject", {"ticket": wrong["ticket"]}).get_json()["ok"]

    r = k.post("/pin", {"employee_code": "E2", "pin": "7395", "reason": "MISIDENTIFIED"}).get_json()
    assert r["ok"] and r["method"] == "PIN" and r["employee"]["name"] == "Bob D."
    assert k.post("/confirm", {"ticket": r["ticket"], "action": "IN"}).get_json()["ok"]

    with app.app_context():
        p = query("SELECT * FROM punches WHERE employee_id = ?", (seed_data["bob"],), one=True)
        inc = query("SELECT kind FROM incidents WHERE kind = 'MISIDENTIFIED'", one=True)
    assert p["method"] == "PIN" and p["reason"] == "MISIDENTIFIED"
    assert inc is not None


def test_camera_failure_pin_punch(kiosk, app):
    k = kiosk["A"]
    k.post("/heartbeat", {"camera_ok": False, "message": "NotReadableError"})
    r = k.post("/pin", {"employee_code": "E3", "pin": "5162", "reason": "CAMERA_FAILURE"}).get_json()
    assert k.post("/confirm", {"ticket": r["ticket"], "action": "IN"}).get_json()["ok"]
    with app.app_context():
        kinds = [i["kind"] for i in query("SELECT kind FROM incidents")]
    assert kinds.count("CAMERA_FAILURE") >= 1


def test_wrong_pin_and_lockout(kiosk):
    k = kiosk["A"]
    for _ in range(5):
        r = k.post("/pin", {"employee_code": "E1", "pin": "0000"})
        assert r.status_code == 401
    # Even the right PIN is refused while locked
    r = k.post("/pin", {"employee_code": "E1", "pin": "4821"})
    assert r.status_code == 423 and r.get_json()["error"] == "LOCKED"


def test_unknown_code_same_error_as_wrong_pin(kiosk):
    r = kiosk["A"].post("/pin", {"employee_code": "NOPE", "pin": "1234"})
    assert r.get_json()["error"] == "BAD_CREDENTIALS"


# ---------------------------------------------------------------------------
# Multi-site rules
# ---------------------------------------------------------------------------
def test_site_change_between_buildings(kiosk):
    assert kiosk["A"].punch_face(RED, "IN").get_json()["ok"]
    r = kiosk["B"].identify(RED).get_json()
    assert r["suggested_action"] == "SITE_CHANGE"
    assert kiosk["B"].post("/confirm", {"ticket": r["ticket"], "action": "SITE_CHANGE"}).get_json()["ok"]
    # Back at building B, SITE_CHANGE is no longer proposed
    r = kiosk["B"].identify(RED).get_json()
    assert "SITE_CHANGE" not in r["allowed_actions"]


def test_site_restriction(kiosk, app, seed_data):
    from faceclock.db import execute
    with app.app_context():
        execute("INSERT INTO employee_sites (employee_id, site_id) VALUES (?, ?)",
                (seed_data["alice"], seed_data["site_a"]))
    # Alice is not a candidate at building B any more
    assert kiosk["B"].identify(RED).get_json()["error"] == "NOT_RECOGNIZED"
    r = kiosk["B"].post("/pin", {"employee_code": "E1", "pin": "4821"}).get_json()
    res = kiosk["B"].post("/confirm", {"ticket": r["ticket"], "action": "IN"})
    assert res.get_json()["error"] == "SITE_NOT_ALLOWED"


# ---------------------------------------------------------------------------
# Offline mode (network / server outage)
# ---------------------------------------------------------------------------
def test_offline_sync_is_idempotent_and_reviewed(kiosk, app, seed_data):
    ts = to_iso(utcnow() - timedelta(hours=2))
    item = {"client_uuid": "u-1", "employee_code": "E1", "pin": "4821", "action": "IN", "ts": ts}
    r1 = kiosk["A"].post("/offline-sync", {"punches": [item]}).get_json()
    r2 = kiosk["A"].post("/offline-sync", {"punches": [item]}).get_json()
    assert r1["results"][0]["ok"] and r2["results"][0]["ok"]
    with app.app_context():
        rows = query("SELECT * FROM punches WHERE employee_id = ?", (seed_data["alice"],))
    assert len(rows) == 1
    assert rows[0]["method"] == "OFFLINE" and rows[0]["status"] == "PENDING_REVIEW"
    assert rows[0]["ts"] == ts                      # original time is preserved


def test_offline_sync_rejects_bad_pin_and_future(kiosk):
    future = to_iso(utcnow() + timedelta(hours=3))
    items = [{"client_uuid": "u-2", "employee_code": "E1", "pin": "9999", "action": "IN",
              "ts": to_iso(utcnow())},
             {"client_uuid": "u-3", "employee_code": "E1", "pin": "4821", "action": "IN",
              "ts": future}]
    res = kiosk["A"].post("/offline-sync", {"punches": items}).get_json()["results"]
    assert {r["error"] for r in res} == {"BAD_CREDENTIALS", "BAD_TIMESTAMP"}
