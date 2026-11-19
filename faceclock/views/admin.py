"""
Back-office (HR / managers / administrators)
============================================

Role model:
    ADMIN    everything, including organisation settings and kiosks
    HR       all employees, timesheets, reviews; no organisation settings
    MANAGER  only employees of their department and its sub-departments
"""

import csv
import io
from datetime import date, timedelta

from flask import (Blueprint, Response, abort, current_app, flash, redirect, render_template,
                   request, session, url_for)

from ..cli import create_kiosk, ensure_defaults
from ..db import execute, from_iso, now_iso, query, utcnow
from ..security import (PinError, authenticate_user, current_user, hash_password, hash_pin,
                        login_required, validate_pin_format, visible_department_ids)
from ..services.audit import audit, verify_chain
from ..services.punches import PUNCH_TYPES, PunchError, record_punch
from ..services.recognition import (RecognitionError, biometrics_available, delete_biometrics,
                                    enroll)
from ..services.timesheet import compute_timesheet, period_totals, presence_now

bp = Blueprint("admin", __name__, url_prefix="/admin")
STAFF = ("ADMIN", "HR", "MANAGER")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _actor():
    return f"user:{current_user()['username']}"


def _scoped_employees(active_only=False):
    """Employees visible to the logged-in user."""
    deps = visible_department_ids(current_user())
    sql = ("SELECT e.*, d.name AS department, s.name AS site, sc.name AS schedule, "
           "(SELECT COUNT(*) FROM face_templates t WHERE t.employee_id = e.id) AS templates "
           "FROM employees e LEFT JOIN departments d ON d.id = e.department_id "
           "LEFT JOIN sites s ON s.id = e.home_site_id "
           "LEFT JOIN schedules sc ON sc.id = e.schedule_id")
    rows = query(sql + (" WHERE e.active = 1" if active_only else "") + " ORDER BY e.last_name")
    if deps is None:
        return rows
    return [r for r in rows if r["department_id"] in deps]


def _employee_or_404(emp_id):
    emp = query("SELECT * FROM employees WHERE id = ?", (emp_id,), one=True)
    if emp is None:
        abort(404)
    deps = visible_department_ids(current_user())
    if deps is not None and emp["department_id"] not in deps:
        abort(403)
    return emp


def _period():
    today = date.today()
    try:
        start = date.fromisoformat(request.args.get("from", ""))
    except ValueError:
        start = today.replace(day=1)
    try:
        end = date.fromisoformat(request.args.get("to", ""))
    except ValueError:
        end = today
    if end < start:
        start, end = end, start
    if (end - start).days > 366:
        end = start + timedelta(days=366)
    return start, end


def _lookups():
    return {
        "sites": query("SELECT * FROM sites ORDER BY name"),
        "departments": query("SELECT d.*, p.name AS parent FROM departments d "
                             "LEFT JOIN departments p ON p.id = d.parent_id ORDER BY d.code"),
        "schedules": query("SELECT * FROM schedules ORDER BY name"),
        "break_types": query("SELECT * FROM break_types ORDER BY id"),
    }


def _int_or_none(value):
    try:
        return int(value) if value not in (None, "") else None
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------
@bp.route("/login", methods=["GET", "POST"])
def login():
    from .setup import setup_needed
    if setup_needed():                       # first start: no account yet
        return redirect(url_for("setup.first_run"))
    if request.method == "POST":
        user = authenticate_user(request.form.get("username", ""), request.form.get("password", ""))
        if user:
            session.clear()
            session["user_id"] = user["id"]
            audit(f"user:{user['username']}", "LOGIN")
            nxt = request.args.get("next", "")
            return redirect(nxt if nxt.startswith("/admin") else url_for("admin.dashboard"))
        flash("Invalid username or password", "error")
    return render_template("login.html", title="Back-office login", action=url_for("admin.login"),
                           user_label="Username", secret_label="Password", secret_name="password",
                           user_name="username")


@bp.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("admin.login"))


# ---------------------------------------------------------------------------
# Dashboard & live presence
# ---------------------------------------------------------------------------
@bp.get("/")
@login_required(*STAFF)
def dashboard():
    deps = visible_department_ids(current_user())
    present = presence_now(department_ids=deps)
    by_site = {}
    for p in present:
        by_site.setdefault(p["site"] or "Unknown", []).append(p)
    kiosks = query("SELECT k.*, s.name AS site FROM kiosks k JOIN sites s ON s.id = k.site_id "
                   "WHERE k.active = 1 ORDER BY s.name, k.name")
    now = utcnow()
    for_kiosks = []
    for k in kiosks:
        online = bool(k["last_seen"]) and (now - from_iso(k["last_seen"])).total_seconds() < 180
        for_kiosks.append(dict(k, online=online))
    stats = {
        "present": len([p for p in present if not p["on_break"]]),
        "on_break": len([p for p in present if p["on_break"]]),
        "pending_punches": query("SELECT COUNT(*) n FROM punches WHERE status = 'PENDING_REVIEW'",
                                 one=True)["n"],
        "pending_corrections": query("SELECT COUNT(*) n FROM correction_requests "
                                     "WHERE status = 'PENDING'", one=True)["n"],
    }
    incidents = query("SELECT i.*, k.name AS kiosk FROM incidents i "
                      "LEFT JOIN kiosks k ON k.id = i.kiosk_id ORDER BY i.id DESC LIMIT 8")
    return render_template("admin/dashboard.html", by_site=by_site, stats=stats,
                           kiosks=for_kiosks, incidents=incidents,
                           biometrics=biometrics_available())


@bp.get("/presence")
@login_required(*STAFF)
def presence():
    """Evacuation roll-call: who is in which building right now (printable)."""
    site_id = _int_or_none(request.args.get("site_id"))
    rows = presence_now(site_id=site_id, department_ids=visible_department_ids(current_user()))
    return render_template("admin/presence.html", rows=rows, site_id=site_id,
                           sites=query("SELECT * FROM sites ORDER BY name"), generated=now_iso())


# ---------------------------------------------------------------------------
# Employees
# ---------------------------------------------------------------------------
@bp.get("/employees")
@login_required(*STAFF)
def employees():
    return render_template("admin/employees.html", employees=_scoped_employees())


def _save_employee_sites(emp_id, site_ids):
    execute("DELETE FROM employee_sites WHERE employee_id = ?", (emp_id,))
    for sid in site_ids:
        execute("INSERT INTO employee_sites (employee_id, site_id) VALUES (?, ?)", (emp_id, sid))


@bp.route("/employees/new", methods=["GET", "POST"])
@login_required("ADMIN", "HR")
def employee_new():
    if request.method == "POST":
        f = request.form
        try:
            validate_pin_format(f.get("pin", ""))
            consent = 1 if f.get("biometric_consent") else 0
            emp_id = execute(
                "INSERT INTO employees (employee_code, first_name, last_name, email, department_id, "
                "home_site_id, schedule_id, pin_hash, biometric_consent, consent_at, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (f["employee_code"].strip(), f["first_name"].strip(), f["last_name"].strip(),
                 f.get("email") or None, _int_or_none(f.get("department_id")),
                 _int_or_none(f.get("home_site_id")), _int_or_none(f.get("schedule_id")),
                 hash_pin(f["pin"]), consent, now_iso() if consent else None, now_iso()))
        except PinError as exc:
            flash(exc.message, "error")
            return render_template("admin/employee_form.html", emp=None, allowed_sites=set(),
                                   **_lookups())
        except Exception as exc:  # unique constraint, missing field ...
            flash(f"Could not create employee: {exc}", "error")
            return render_template("admin/employee_form.html", emp=None, allowed_sites=set(),
                                   **_lookups())
        _save_employee_sites(emp_id, [int(s) for s in f.getlist("site_ids")])
        audit(_actor(), "EMPLOYEE_CREATED", "employee", emp_id)
        flash("Employee created", "ok")
        return redirect(url_for("admin.employee_edit", emp_id=emp_id))
    return render_template("admin/employee_form.html", emp=None, allowed_sites=set(), **_lookups())


@bp.route("/employees/<int:emp_id>", methods=["GET", "POST"])
@login_required(*STAFF)
def employee_edit(emp_id):
    emp = _employee_or_404(emp_id)
    if request.method == "POST":
        if current_user()["role"] == "MANAGER":
            abort(403)
        f = request.form
        consent = 1 if f.get("biometric_consent") else 0
        execute("UPDATE employees SET first_name=?, last_name=?, email=?, department_id=?, "
                "home_site_id=?, schedule_id=?, biometric_consent=?, consent_at=?, active=? "
                "WHERE id=?",
                (f["first_name"].strip(), f["last_name"].strip(), f.get("email") or None,
                 _int_or_none(f.get("department_id")), _int_or_none(f.get("home_site_id")),
                 _int_or_none(f.get("schedule_id")), consent,
                 (emp["consent_at"] or now_iso()) if consent else None,
                 1 if f.get("active") else 0, emp_id))
        _save_employee_sites(emp_id, [int(s) for s in f.getlist("site_ids")])
        if not consent and emp["biometric_consent"]:
            delete_biometrics(emp_id, _actor())          # consent withdrawn => erase templates
            flash("Consent withdrawn: face templates erased", "ok")
        audit(_actor(), "EMPLOYEE_UPDATED", "employee", emp_id)
        flash("Employee saved", "ok")
        return redirect(url_for("admin.employee_edit", emp_id=emp_id))
    allowed = {r["site_id"] for r in query("SELECT site_id FROM employee_sites WHERE employee_id = ?",
                                           (emp_id,))}
    templates = query("SELECT COUNT(*) n FROM face_templates WHERE employee_id = ?",
                      (emp_id,), one=True)["n"]
    return render_template("admin/employee_form.html", emp=emp, allowed_sites=allowed,
                           templates=templates, biometrics=biometrics_available(), **_lookups())


@bp.post("/employees/<int:emp_id>/pin")
@login_required("ADMIN", "HR")
def employee_reset_pin(emp_id):
    _employee_or_404(emp_id)
    pin = request.form.get("pin", "")
    try:
        validate_pin_format(pin)
    except PinError as exc:
        flash(exc.message, "error")
        return redirect(url_for("admin.employee_edit", emp_id=emp_id))
    execute("UPDATE employees SET pin_hash = ?, pin_failed_attempts = 0, pin_locked_until = NULL "
            "WHERE id = ?", (hash_pin(pin), emp_id))
    audit(_actor(), "PIN_RESET", "employee", emp_id)
    flash("PIN updated", "ok")
    return redirect(url_for("admin.employee_edit", emp_id=emp_id))


@bp.post("/employees/<int:emp_id>/enroll")
@login_required("ADMIN", "HR")
def employee_enroll(emp_id):
    emp = _employee_or_404(emp_id)
    frames = (request.get_json(silent=True) or {}).get("frames") or []
    try:
        n = enroll(emp, frames, _actor())
    except RecognitionError as exc:
        return {"ok": False, "error": exc.code, "message": exc.message}, 422
    return {"ok": True, "samples": n}


@bp.post("/employees/<int:emp_id>/biometrics/delete")
@login_required("ADMIN", "HR")
def employee_delete_biometrics(emp_id):
    _employee_or_404(emp_id)
    delete_biometrics(emp_id, _actor())
    flash("Face templates deleted", "ok")
    return redirect(url_for("admin.employee_edit", emp_id=emp_id))


# ---------------------------------------------------------------------------
# Organisation (sites, departments, schedules, breaks, kiosks, users)
# ---------------------------------------------------------------------------
@bp.route("/organization", methods=["GET", "POST"])
@login_required("ADMIN")
def organization():
    new_token = None
    if request.method == "POST":
        f, kind = request.form, request.form.get("kind")
        try:
            if kind == "site":
                execute("INSERT INTO sites (name, address, timezone) VALUES (?, ?, ?)",
                        (f["name"], f.get("address"), f.get("timezone") or "Europe/Paris"))
            elif kind == "department":
                execute("INSERT INTO departments (name, code, parent_id) VALUES (?, ?, ?)",
                        (f["name"], f["code"].upper(), _int_or_none(f.get("parent_id"))))
            elif kind == "schedule":
                execute("INSERT INTO schedules (name, start_time, end_time, workdays, grace_minutes, "
                        "expected_minutes) VALUES (?, ?, ?, ?, ?, ?)",
                        (f["name"], f["start_time"], f["end_time"], f.get("workdays") or "1,2,3,4,5",
                         int(f.get("grace_minutes") or 5), int(f.get("expected_minutes") or 420)))
            elif kind == "break_type":
                execute("INSERT INTO break_types (code, label, paid, daily_allowance_minutes, "
                        "max_duration_minutes) VALUES (?, ?, ?, ?, ?) ON CONFLICT(code) DO UPDATE SET "
                        "label=excluded.label, paid=excluded.paid, "
                        "daily_allowance_minutes=excluded.daily_allowance_minutes, "
                        "max_duration_minutes=excluded.max_duration_minutes",
                        (f["code"].upper(), f["label"], 1 if f.get("paid") else 0,
                         int(f.get("daily_allowance_minutes") or 0),
                         int(f.get("max_duration_minutes") or 0)))
            elif kind == "kiosk":
                _, new_token = create_kiosk(f["name"], int(f["site_id"]))
            elif kind == "user":
                execute("INSERT INTO users (username, password_hash, role, department_id) "
                        "VALUES (?, ?, ?, ?)",
                        (f["username"], hash_password(f["password"]), f["role"],
                         _int_or_none(f.get("department_id"))))
            elif kind == "kiosk_disable":
                execute("UPDATE kiosks SET active = 0 WHERE id = ?", (int(f["kiosk_id"]),))
            audit(_actor(), f"ORG_{kind.upper()}_SAVED", details={k: v for k, v in f.items()
                                                                 if k not in ("password", "_csrf")})
            flash("Saved", "ok")
        except Exception as exc:
            flash(f"Error: {exc}", "error")
    ensure_defaults()
    kiosks = query("SELECT k.*, s.name AS site FROM kiosks k JOIN sites s ON s.id = k.site_id "
                   "ORDER BY s.name, k.name")
    users = query("SELECT u.*, d.name AS department FROM users u "
                  "LEFT JOIN departments d ON d.id = u.department_id ORDER BY u.username")
    return render_template("admin/organization.html", kiosks=kiosks, users=users,
                           new_token=new_token, **_lookups())


# ---------------------------------------------------------------------------
# Timesheets & exports
# ---------------------------------------------------------------------------
def _timesheet_rows(start, end):
    dep_filter = _int_or_none(request.args.get("department_id"))
    rows = []
    for emp in _scoped_employees(active_only=True):
        if dep_filter and emp["department_id"] != dep_filter:
            continue
        days = compute_timesheet(emp, start, end)
        rows.append({"emp": emp, "totals": period_totals(days), "days": days})
    return rows


@bp.get("/timesheets")
@login_required(*STAFF)
def timesheets():
    start, end = _period()
    return render_template("admin/timesheets.html", rows=_timesheet_rows(start, end),
                           start=start, end=end, **_lookups())


@bp.get("/timesheets/<int:emp_id>")
@login_required(*STAFF)
def timesheet_detail(emp_id):
    emp = _employee_or_404(emp_id)
    start, end = _period()
    days = compute_timesheet(emp, start, end)
    punches = query(
        "SELECT p.*, s.name AS site, bt.label AS break_label FROM punches p "
        "LEFT JOIN sites s ON s.id = p.site_id LEFT JOIN break_types bt ON bt.id = p.break_type_id "
        "WHERE p.employee_id = ? AND p.ts >= ? ORDER BY p.ts DESC LIMIT 300",
        (emp_id, start.isoformat()))
    return render_template("admin/timesheet_detail.html", emp=emp, days=days,
                           totals=period_totals(days), punches=punches, start=start, end=end,
                           punch_types=PUNCH_TYPES, **_lookups())


@bp.get("/timesheets/export.csv")
@login_required(*STAFF)
def timesheets_export():
    start, end = _period()
    out = io.StringIO()
    out.write("\ufeff")                     # UTF-8 BOM so that Excel opens accents correctly
    w = csv.writer(out, delimiter=";")
    w.writerow(["employee_code", "last_name", "first_name", "department", "day", "first_in",
                "last_out", "presence_min", "worked_min", "paid_break_min", "unpaid_break_min",
                "late_min", "early_leave_min", "overtime_min", "expected_min", "manual_punches",
                "anomalies"])
    for r in _timesheet_rows(start, end):
        e = r["emp"]
        for d in r["days"]:
            w.writerow([e["employee_code"], e["last_name"], e["first_name"], e["department"],
                        d.day.isoformat(), d.first_in or "", d.last_out or "", d.presence_min,
                        d.worked_min, d.paid_break_min, d.unpaid_break_min, d.late_min,
                        d.early_leave_min, d.overtime_min, d.expected_min, d.pin_punches,
                        " ".join(d.anomalies)])
    audit(_actor(), "TIMESHEET_EXPORT", details={"from": start.isoformat(), "to": end.isoformat()})
    return Response(out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition":
                             f"attachment; filename=timesheets_{start}_{end}.csv"})


# ---------------------------------------------------------------------------
# Manual punches, review queue, correction requests
# ---------------------------------------------------------------------------
@bp.post("/punches/manual")
@login_required(*STAFF)
def punch_manual():
    f = request.form
    emp = _employee_or_404(int(f["employee_id"]))
    reason = (f.get("note") or "").strip()
    if not reason:
        flash("A justification is mandatory for a manual punch", "error")
        return redirect(request.referrer or url_for("admin.timesheets"))
    try:
        ts = _local_to_utc(f["ts"], emp)
        record_punch(emp, f["punch_type"], site_id=_int_or_none(f.get("site_id")), method="ADMIN",
                     ts=ts, break_code=f.get("break_type") or None, note=reason,
                     created_by=_actor(), enforce_rules=False)
        flash("Punch added", "ok")
    except (PunchError, ValueError, KeyError) as exc:
        flash(f"Error: {getattr(exc, 'message', exc)}", "error")
    return redirect(request.referrer or url_for("admin.timesheets"))


def _local_to_utc(value, emp):
    """Convert a 'YYYY-MM-DDTHH:MM' value typed in the employee's site timezone."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    tz_name = "Europe/Paris"
    if emp["home_site_id"]:
        site = query("SELECT timezone FROM sites WHERE id = ?", (emp["home_site_id"],), one=True)
        tz_name = site["timezone"] if site else tz_name
    return datetime.fromisoformat(value).replace(tzinfo=ZoneInfo(tz_name))


@bp.get("/review")
@login_required(*STAFF)
def review():
    deps = visible_department_ids(current_user())

    def scoped(rows):
        return [r for r in rows if deps is None or r["department_id"] in deps]

    punches = scoped(query(
        "SELECT p.*, e.first_name, e.last_name, e.employee_code, e.department_id, s.name AS site "
        "FROM punches p JOIN employees e ON e.id = p.employee_id "
        "LEFT JOIN sites s ON s.id = p.site_id WHERE p.status = 'PENDING_REVIEW' ORDER BY p.ts"))
    corrections = scoped(query(
        "SELECT c.*, e.first_name, e.last_name, e.employee_code, e.department_id, "
        "p.punch_type AS current_type, p.ts AS current_ts FROM correction_requests c "
        "JOIN employees e ON e.id = c.employee_id LEFT JOIN punches p ON p.id = c.punch_id "
        "WHERE c.status = 'PENDING' ORDER BY c.created_at"))
    return render_template("admin/review.html", punches=punches, corrections=corrections)


@bp.post("/punches/<int:punch_id>/review")
@login_required(*STAFF)
def punch_review(punch_id):
    punch = query("SELECT * FROM punches WHERE id = ?", (punch_id,), one=True) or abort(404)
    _employee_or_404(punch["employee_id"])
    decision = "VALID" if request.form.get("decision") == "approve" else "REJECTED"
    execute("UPDATE punches SET status = ? WHERE id = ?", (decision, punch_id))
    audit(_actor(), "PUNCH_REVIEWED", "punch", punch_id, {"status": decision})
    return redirect(url_for("admin.review"))


@bp.post("/corrections/<int:req_id>/review")
@login_required(*STAFF)
def correction_review(req_id):
    req = query("SELECT * FROM correction_requests WHERE id = ? AND status = 'PENDING'",
                (req_id,), one=True) or abort(404)
    emp = _employee_or_404(req["employee_id"])
    approve = request.form.get("decision") == "approve"
    note = request.form.get("review_note", "")
    if approve:
        try:
            if req["action"] in ("MODIFY", "DELETE") and req["punch_id"]:
                execute("UPDATE punches SET status = 'REJECTED', note = COALESCE(note,'') || ? "
                        "WHERE id = ?", (f" [superseded by correction #{req_id}]", req["punch_id"]))
            if req["action"] in ("ADD", "MODIFY"):
                bt = None
                if req["break_type_id"]:
                    bt = query("SELECT code FROM break_types WHERE id = ?",
                               (req["break_type_id"],), one=True)["code"]
                record_punch(emp, req["proposed_type"], site_id=emp["home_site_id"],
                             method="ADMIN", ts=from_iso(req["proposed_ts"]), break_code=bt,
                             note=f"Correction #{req_id}: {req['reason']}", created_by=_actor(),
                             enforce_rules=False)
        except PunchError as exc:
            flash(exc.message, "error")
            return redirect(url_for("admin.review"))
    execute("UPDATE correction_requests SET status = ?, reviewer = ?, review_note = ?, "
            "reviewed_at = ? WHERE id = ?",
            ("APPROVED" if approve else "REJECTED", current_user()["username"], note, now_iso(),
             req_id))
    audit(_actor(), "CORRECTION_REVIEWED", "correction", req_id, {"approved": approve})
    flash("Correction processed", "ok")
    return redirect(url_for("admin.review"))


# ---------------------------------------------------------------------------
# Audit log & IT incidents
# ---------------------------------------------------------------------------
@bp.get("/audit")
@login_required("ADMIN", "HR")
def audit_view():
    ok, broken = verify_chain()
    entries = query("SELECT * FROM audit_log ORDER BY id DESC LIMIT 300")
    incidents = query("SELECT i.*, k.name AS kiosk, s.name AS site FROM incidents i "
                      "LEFT JOIN kiosks k ON k.id = i.kiosk_id LEFT JOIN sites s ON s.id = k.site_id "
                      "ORDER BY i.id DESC LIMIT 200")
    return render_template("admin/audit.html", entries=entries, incidents=incidents,
                           chain_ok=ok, broken=broken)
