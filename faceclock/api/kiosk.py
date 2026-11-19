"""
Kiosk API
=========

Used by the punch terminals installed at each building entrance. Every call is
authenticated with the kiosk token (``X-Kiosk-Token`` header).

Typical flow::

    1. POST /identify   frames            -> ticket + "Hello Marie D." + suggested action
    2. employee presses ENTER (confirm) or ESC ("not me")
    3a. POST /confirm  ticket + action    -> punch recorded (method FACE)
    3b. POST /reject   ticket             -> misidentification logged, PIN form shown
    4. POST /pin       code + PIN         -> ticket (method PIN) -> POST /confirm

Fallbacks for IT failures:
    * camera broken / no biometric consent  -> PIN flow
    * network or server down                -> punches queued in the browser,
                                               then POST /offline-sync
"""

import json
import secrets

from flask import Blueprint, current_app, g, request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from ..db import execute, from_iso, now_iso, query, utcnow
from ..security import PinError, kiosk_required, verify_employee_pin
from ..services.audit import audit
from ..services.punches import (PunchError, allowed_actions, get_state, record_punch,
                                suggested_action)
from ..services.recognition import RecognitionError, biometrics_available, identify

bp = Blueprint("kiosk_api", __name__, url_prefix="/api/kiosk")

FALLBACK_REASONS = {"CAMERA_FAILURE", "NOT_RECOGNIZED", "MISIDENTIFIED",
                    "NO_BIOMETRIC_CONSENT", "USER_CHOICE", "ENGINE_UNAVAILABLE"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _serializer():
    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt="kiosk-ticket")


def _issue_ticket(employee_id, method, confidence=None, reason=None):
    return _serializer().dumps({"e": employee_id, "k": g.kiosk["id"], "m": method,
                                "c": confidence, "r": reason, "n": secrets.token_hex(8)})


def _read_ticket(token, consume=False):
    try:
        data = _serializer().loads(token or "", max_age=current_app.config["TICKET_TTL_SECONDS"])
    except SignatureExpired:
        raise PunchError("TICKET_EXPIRED", "Too slow, please start again")
    except BadSignature:
        raise PunchError("BAD_TICKET", "Invalid confirmation ticket")
    if data["k"] != g.kiosk["id"]:
        raise PunchError("BAD_TICKET", "Ticket issued by another kiosk")
    if consume:
        try:
            execute("INSERT INTO used_tickets (nonce, used_at) VALUES (?, ?)",
                    (data["n"], now_iso()))
        except Exception:
            raise PunchError("TICKET_USED", "This confirmation was already used")
    return data


def _display_name(emp):
    # Privacy on a shared screen: first name + last-name initial only.
    return f"{emp['first_name']} {emp['last_name'][:1]}."


def _employee_payload(emp, method, confidence=None, reason=None):
    site_id = g.kiosk["site_id"]
    state = get_state(emp["id"])
    return {
        "ok": True,
        "ticket": _issue_ticket(emp["id"], method, confidence, reason),
        "ttl": current_app.config["TICKET_TTL_SECONDS"],
        "employee": {"name": _display_name(emp), "code": emp["employee_code"]},
        "method": method,
        "confidence": round(confidence, 3) if confidence is not None else None,
        "state": state["state"],
        "on_break_type": state["break_code"],
        "previous_shift_forgotten": state["forgotten"],
        "allowed_actions": allowed_actions(state, site_id),
        "suggested_action": suggested_action(state, site_id),
    }


def _log_incident(kind, details=None):
    execute("INSERT INTO incidents (kiosk_id, kind, details, ts) VALUES (?, ?, ?, ?)",
            (g.kiosk["id"], kind, json.dumps(details or {}), now_iso()))


def _error(code, message, status=400):
    return {"ok": False, "error": code, "message": message}, status


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@bp.get("/config")
@kiosk_required
def kiosk_config():
    breaks = query("SELECT code, label, paid FROM break_types WHERE active = 1 ORDER BY id")
    return {
        "ok": True,
        "kiosk": {"id": g.kiosk["id"], "name": g.kiosk["name"]},
        "site": {"id": g.kiosk["site_id"], "name": g.kiosk["site_name"],
                 "timezone": g.kiosk["site_tz"]},
        "biometrics": biometrics_available(),
        "liveness": current_app.config["LIVENESS_ENABLED"],
        "break_types": [dict(b) for b in breaks],
        "server_time": now_iso(),
    }


@bp.post("/heartbeat")
@kiosk_required
def heartbeat():
    payload = request.get_json(silent=True) or {}
    camera_ok = payload.get("camera_ok")
    if camera_ok is not None:
        previous = g.kiosk["camera_ok"]
        execute("UPDATE kiosks SET camera_ok = ? WHERE id = ?", (int(bool(camera_ok)), g.kiosk["id"]))
        if previous in (None, 1) and not camera_ok:
            _log_incident("CAMERA_FAILURE", {"message": payload.get("message")})
    return {"ok": True, "server_time": now_iso(), "biometrics": biometrics_available()}


@bp.post("/identify")
@kiosk_required
def identify_face():
    payload = request.get_json(silent=True) or {}
    frames = payload.get("frames") or ([payload["image"]] if payload.get("image") else [])
    try:
        emp, match = identify(frames, g.kiosk["site_id"])
    except RecognitionError as exc:
        if exc.code in ("NOT_RECOGNIZED", "AMBIGUOUS", "LIVENESS_FAILED"):
            _log_incident(f"FACE_{exc.code}")
        return _error(exc.code, exc.message, 422)
    return _employee_payload(emp, "FACE", confidence=match.score)


@bp.post("/reject")
@kiosk_required
def reject_identification():
    """The employee pressed "Not me": record it to monitor recognition quality."""
    payload = request.get_json(silent=True) or {}
    try:
        data = _read_ticket(payload.get("ticket"), consume=True)
    except PunchError as exc:
        return _error(exc.code, exc.message)
    _log_incident("MISIDENTIFIED", {"proposed_employee_id": data["e"], "score": data["c"]})
    audit(f"kiosk:{g.kiosk['id']}", "MISIDENTIFICATION", "employee", data["e"],
          {"score": data["c"]})
    return {"ok": True}


@bp.post("/pin")
@kiosk_required
def pin_login():
    """Manual identification with employee code + secret PIN."""
    payload = request.get_json(silent=True) or {}
    reason = (payload.get("reason") or "USER_CHOICE").upper()
    if reason not in FALLBACK_REASONS:
        reason = "USER_CHOICE"
    try:
        emp = verify_employee_pin(str(payload.get("employee_code", "")), str(payload.get("pin", "")))
    except PinError as exc:
        return _error(exc.code, exc.message, 401 if exc.code != "LOCKED" else 423)
    if reason == "CAMERA_FAILURE":
        _log_incident("CAMERA_FAILURE", {"employee_id": emp["id"]})
    return _employee_payload(emp, "PIN", reason=reason)


@bp.post("/confirm")
@kiosk_required
def confirm_punch():
    payload = request.get_json(silent=True) or {}
    try:
        data = _read_ticket(payload.get("ticket"), consume=True)
        emp = query("SELECT * FROM employees WHERE id = ? AND active = 1", (data["e"],), one=True)
        if emp is None:
            raise PunchError("UNKNOWN_EMPLOYEE", "Employee not found")
        result = record_punch(
            emp, payload.get("action"), site_id=g.kiosk["site_id"], kiosk_id=g.kiosk["id"],
            method=data["m"], break_code=payload.get("break_type"), reason=data.get("r"),
            confidence=data.get("c"), created_by=f"kiosk:{g.kiosk['id']}")
    except PunchError as exc:
        return _error(exc.code, exc.message, 409)
    return {"ok": True, "punch": result, "employee": {"name": _display_name(emp)}}


@bp.post("/offline-sync")
@kiosk_required
def offline_sync():
    """Replay punches queued by the kiosk while the server was unreachable.

    Each item: ``{client_uuid, employee_code, pin, action, break_type, ts}``.
    Items are processed in chronological order and are idempotent.
    """
    cfg = current_app.config
    items = (request.get_json(silent=True) or {}).get("punches") or []
    results = []
    for item in sorted(items, key=lambda i: i.get("ts", "")):
        uid = item.get("client_uuid")
        try:
            ts = from_iso(item["ts"])
            age_h = (utcnow() - ts).total_seconds() / 3600
            if age_h > cfg["OFFLINE_MAX_AGE_HOURS"] or age_h < -0.1:
                raise PunchError("BAD_TIMESTAMP", "Offline punch timestamp out of range")
            emp = verify_employee_pin(str(item.get("employee_code", "")), str(item.get("pin", "")))
            res = record_punch(emp, item.get("action"), site_id=g.kiosk["site_id"],
                               kiosk_id=g.kiosk["id"], method="OFFLINE", ts=ts,
                               break_code=item.get("break_type"), reason="SERVER_UNREACHABLE",
                               client_uuid=uid, created_by=f"kiosk:{g.kiosk['id']}")
            results.append({"client_uuid": uid, "ok": True, "status": res["status"]})
        except (PunchError, PinError) as exc:
            results.append({"client_uuid": uid, "ok": False, "error": exc.code})
        except (KeyError, ValueError):
            results.append({"client_uuid": uid, "ok": False, "error": "BAD_ITEM"})
    if items:
        _log_incident("OFFLINE_SYNC", {"count": len(items),
                                       "failed": sum(1 for r in results if not r["ok"])})
    return {"ok": True, "results": results}


@bp.post("/incident")
@kiosk_required
def report_incident():
    payload = request.get_json(silent=True) or {}
    kind = str(payload.get("kind", "OTHER")).upper()[:40]
    _log_incident(kind, {"message": str(payload.get("message", ""))[:500]})
    return {"ok": True}
