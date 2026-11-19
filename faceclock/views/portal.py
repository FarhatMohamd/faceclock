"""
Employee self-service portal
============================

Employees log in with their code + secret PIN to:
  * see their own punches and daily worked time,
  * request a correction (forgotten punch, wrong action) -> manager approval,
  * view their personal data and withdraw biometric consent (GDPR rights).
"""

from datetime import date, datetime, timedelta
from functools import wraps
from zoneinfo import ZoneInfo

from flask import Blueprint, flash, redirect, render_template, request, session, url_for

from ..db import execute, now_iso, query, to_iso
from ..security import PinError, verify_employee_pin
from ..services.audit import audit
from ..services.punches import PUNCH_TYPES
from ..services.recognition import delete_biometrics
from ..services.timesheet import compute_timesheet, period_totals

bp = Blueprint("portal", __name__, url_prefix="/me")


def employee_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        emp_id = session.get("employee_id")
        emp = query("SELECT * FROM employees WHERE id = ? AND active = 1", (emp_id,), one=True) \
            if emp_id else None
        if emp is None:
            return redirect(url_for("portal.login"))
        return view(emp, *args, **kwargs)

    return wrapper


@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        try:
            emp = verify_employee_pin(request.form.get("employee_code", ""),
                                      request.form.get("pin", ""))
            session.clear()
            session["employee_id"] = emp["id"]
            return redirect(url_for("portal.home"))
        except PinError as exc:
            flash(exc.message, "error")
    return render_template("login.html", title="My time & attendance", action=url_for("portal.login"),
                           user_label="Employee code", user_name="employee_code",
                           secret_label="Secret PIN", secret_name="pin")


@bp.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("portal.login"))


@bp.get("/")
@employee_required
def home(emp):
    end = date.today()
    start = end - timedelta(days=30)
    days = compute_timesheet(emp, start, end)
    punches = query(
        "SELECT p.*, s.name AS site, bt.label AS break_label FROM punches p "
        "LEFT JOIN sites s ON s.id = p.site_id LEFT JOIN break_types bt ON bt.id = p.break_type_id "
        "WHERE p.employee_id = ? ORDER BY p.ts DESC LIMIT 60", (emp["id"],))
    requests_ = query("SELECT * FROM correction_requests WHERE employee_id = ? "
                      "ORDER BY id DESC LIMIT 20", (emp["id"],))
    templates = query("SELECT COUNT(*) n FROM face_templates WHERE employee_id = ?",
                      (emp["id"],), one=True)["n"]
    return render_template("portal/home.html", emp=emp, days=list(reversed(days)),
                           totals=period_totals(days), punches=punches, requests=requests_,
                           templates=templates, punch_types=PUNCH_TYPES,
                           break_types=query("SELECT * FROM break_types WHERE active = 1"))


@bp.post("/corrections")
@employee_required
def request_correction(emp):
    f = request.form
    reason = (f.get("reason") or "").strip()
    action = f.get("action", "ADD")
    if not reason or action not in ("ADD", "MODIFY", "DELETE"):
        flash("Please choose an action and explain the reason", "error")
        return redirect(url_for("portal.home"))
    proposed_ts = None
    if action != "DELETE":
        try:
            tz = ZoneInfo("Europe/Paris")
            if emp["home_site_id"]:
                site = query("SELECT timezone FROM sites WHERE id = ?", (emp["home_site_id"],), one=True)
                tz = ZoneInfo(site["timezone"])
            proposed_ts = to_iso(datetime.fromisoformat(f["ts"]).replace(tzinfo=tz))
        except (KeyError, ValueError):
            flash("Please give a valid date and time", "error")
            return redirect(url_for("portal.home"))
    punch_id = int(f["punch_id"]) if f.get("punch_id") else None
    if punch_id and not query("SELECT id FROM punches WHERE id = ? AND employee_id = ?",
                              (punch_id, emp["id"]), one=True):
        flash("Unknown punch", "error")
        return redirect(url_for("portal.home"))
    bt = query("SELECT id FROM break_types WHERE code = ?", (f.get("break_type", ""),), one=True)
    rid = execute(
        "INSERT INTO correction_requests (employee_id, punch_id, action, proposed_type, proposed_ts, "
        "break_type_id, reason, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (emp["id"], punch_id, action, f.get("punch_type") if action != "DELETE" else None,
         proposed_ts, bt["id"] if bt else None, reason[:500], now_iso()))
    audit(f"employee:{emp['employee_code']}", "CORRECTION_REQUESTED", "correction", rid)
    flash("Request sent to your manager", "ok")
    return redirect(url_for("portal.home"))


@bp.post("/consent/withdraw")
@employee_required
def withdraw_consent(emp):
    execute("UPDATE employees SET biometric_consent = 0, consent_at = NULL WHERE id = ?", (emp["id"],))
    delete_biometrics(emp["id"], f"employee:{emp['employee_code']}")
    flash("Your face data has been erased. You can keep clocking in with your code and PIN.", "ok")
    return redirect(url_for("portal.home"))
