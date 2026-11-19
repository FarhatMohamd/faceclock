"""
Punch service: the clock-in / clock-out state machine
=====================================================

States and allowed transitions::

    OFF ──IN──▶ WORKING ──BREAK_START──▶ ON_BREAK
     ▲            │  ▲                       │
     └────OUT─────┘  └──────BREAK_END────────┘
                  │
                  └──SITE_CHANGE (moved to another building, time unaffected)

* ``OUT`` while ``ON_BREAK`` automatically closes the break first.
* A shift left open longer than ``MAX_SHIFT_HOURS`` is considered forgotten:
  the employee can clock in again and the timesheet flags ``MISSING_OUT``.
* An identical punch repeated within ``DEBOUNCE_SECONDS`` is refused
  (double detection in front of the camera).
"""

from __future__ import annotations

import uuid
from datetime import timedelta

from flask import current_app

from ..db import execute, from_iso, now_iso, query, to_iso, utcnow
from .audit import audit

PUNCH_TYPES = ("IN", "OUT", "BREAK_START", "BREAK_END", "SITE_CHANGE")
METHODS = ("FACE", "PIN", "OFFLINE", "ADMIN")

TRANSITIONS = {
    "OFF": ("IN",),
    "WORKING": ("BREAK_START", "OUT", "SITE_CHANGE"),
    "ON_BREAK": ("BREAK_END", "OUT"),
}


class PunchError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
def get_state(employee_id, at=None):
    """Return the employee's clock state at time ``at`` (default: now)."""
    at = at or utcnow()
    last = query(
        "SELECT p.*, bt.code AS break_code FROM punches p "
        "LEFT JOIN break_types bt ON bt.id = p.break_type_id "
        "WHERE p.employee_id = ? AND p.status != 'REJECTED' AND p.ts <= ? "
        "ORDER BY p.ts DESC, p.id DESC LIMIT 1",
        (employee_id, to_iso(at)), one=True)

    state = {"state": "OFF", "last": last, "site_id": None, "forgotten": False,
             "break_code": None, "since": None}
    if last is None or last["punch_type"] == "OUT":
        return state

    state["state"] = "ON_BREAK" if last["punch_type"] == "BREAK_START" else "WORKING"
    state["site_id"] = last["site_id"]
    state["break_code"] = last["break_code"] if state["state"] == "ON_BREAK" else None
    state["since"] = last["ts"]

    max_age = timedelta(hours=current_app.config["MAX_SHIFT_HOURS"])
    if at - from_iso(last["ts"]) > max_age:
        state.update(state="OFF", forgotten=True, site_id=None)
    return state


def allowed_actions(state, kiosk_site_id=None):
    actions = list(TRANSITIONS[state["state"]])
    # SITE_CHANGE only makes sense when arriving at a *different* building
    if "SITE_CHANGE" in actions and (kiosk_site_id is None or state["site_id"] == kiosk_site_id):
        actions.remove("SITE_CHANGE")
    return actions


def suggested_action(state, kiosk_site_id=None):
    """Most likely action, pre-selected on the kiosk (confirm with one key)."""
    if state["state"] == "OFF":
        return "IN"
    if state["state"] == "ON_BREAK":
        return "BREAK_END"
    if kiosk_site_id is not None and state["site_id"] not in (None, kiosk_site_id):
        return "SITE_CHANGE"
    return "OUT"


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------
def employee_can_use_site(employee_id, site_id) -> bool:
    rows = query("SELECT site_id FROM employee_sites WHERE employee_id = ?", (employee_id,))
    return not rows or site_id in {r["site_id"] for r in rows}


def get_break_type(code):
    bt = query("SELECT * FROM break_types WHERE code = ? AND active = 1",
               ((code or "").upper(),), one=True)
    if bt is None:
        raise PunchError("BAD_BREAK_TYPE", f"Unknown break type '{code}'")
    return bt


def _check_debounce(employee_id, punch_type, ts):
    window = current_app.config["DEBOUNCE_SECONDS"]
    recent = query(
        "SELECT id FROM punches WHERE employee_id = ? AND punch_type = ? "
        "AND status != 'REJECTED' AND ts BETWEEN ? AND ?",
        (employee_id, punch_type, to_iso(ts - timedelta(seconds=window)),
         to_iso(ts + timedelta(seconds=window))), one=True)
    if recent:
        raise PunchError("DUPLICATE", "This punch was already recorded a moment ago")


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------
def _insert(employee_id, punch_type, ts, *, site_id, kiosk_id, method, break_type_id=None,
            reason=None, confidence=None, status="VALID", client_uuid=None,
            created_by="kiosk", note=None):
    return execute(
        "INSERT INTO punches (employee_id, site_id, kiosk_id, punch_type, break_type_id, ts, "
        "method, reason, confidence, status, client_uuid, note, created_by, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (employee_id, site_id, kiosk_id, punch_type, break_type_id, to_iso(ts), method,
         reason, confidence, status, client_uuid, note, created_by, now_iso()))


def record_punch(employee, punch_type, *, site_id, kiosk_id=None, method="FACE", ts=None,
                 break_code=None, reason=None, confidence=None, status=None,
                 client_uuid=None, created_by="kiosk", note=None, enforce_rules=True):
    """Validate and store a punch. Returns a dict describing what was recorded."""
    cfg = current_app.config
    punch_type = (punch_type or "").upper()
    if punch_type not in PUNCH_TYPES:
        raise PunchError("BAD_TYPE", f"Unknown punch type '{punch_type}'")
    if method not in METHODS:
        raise PunchError("BAD_METHOD", f"Unknown method '{method}'")

    # Idempotency: an offline punch re-sent twice is recorded only once.
    if client_uuid:
        existing = query("SELECT * FROM punches WHERE client_uuid = ?", (client_uuid,), one=True)
        if existing:
            return {"id": existing["id"], "punch_type": existing["punch_type"],
                    "status": existing["status"], "duplicate": True}

    ts = ts or utcnow()
    emp_id = employee["id"]

    if site_id is not None and not employee_can_use_site(emp_id, site_id):
        raise PunchError("SITE_NOT_ALLOWED", "You are not authorised to clock in at this site")

    break_type_id = None
    if punch_type == "BREAK_START":
        break_type_id = get_break_type(break_code or "COFFEE")["id"]

    state = get_state(emp_id, at=ts)
    auto_closed_break = False
    if enforce_rules:
        if punch_type not in TRANSITIONS[state["state"]]:
            raise PunchError(
                "INVALID_TRANSITION",
                f"Action {punch_type} is not possible while {state['state'].replace('_', ' ').lower()}")
        _check_debounce(emp_id, punch_type, ts)

    if status is None:
        status = "VALID"
        if method == "OFFLINE" and cfg["OFFLINE_REQUIRES_REVIEW"]:
            status = "PENDING_REVIEW"
        if method == "PIN" and cfg["PIN_REQUIRES_REVIEW"]:
            status = "PENDING_REVIEW"

    common = dict(site_id=site_id, kiosk_id=kiosk_id, method=method, reason=reason,
                  confidence=confidence, status=status, created_by=created_by)

    # Leaving while still on break: close the break at the same instant.
    if punch_type == "OUT" and state["state"] == "ON_BREAK":
        _insert(emp_id, "BREAK_END", ts, note="Automatically closed at clock-out", **common)
        auto_closed_break = True

    punch_id = _insert(emp_id, punch_type, ts, break_type_id=break_type_id,
                       client_uuid=client_uuid, note=note, **common)
    audit(created_by, "PUNCH", "punch", punch_id,
          {"employee_id": emp_id, "type": punch_type, "method": method, "site_id": site_id,
           "status": status, "reason": reason})
    return {"id": punch_id, "punch_type": punch_type, "status": status, "ts": to_iso(ts),
            "auto_closed_break": auto_closed_break, "duplicate": False,
            "previous_shift_forgotten": state["forgotten"]}


def new_client_uuid() -> str:
    return str(uuid.uuid4())
