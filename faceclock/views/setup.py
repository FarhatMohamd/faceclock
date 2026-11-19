"""
First-run setup and desktop helpers
===================================

* /setup         - shown only while no back-office user exists: creates the
                   first building and the administrator account, then logs in.
                   Replaces the command line for non-technical users.
* /desktop/kiosk - desktop edition only: opens the punch terminal of this PC
                   (its kiosk is created automatically the first time).
"""

import json
from pathlib import Path

from flask import (Blueprint, abort, current_app, flash, redirect, render_template, request,
                   session, url_for)

from ..cli import create_kiosk, ensure_defaults
from ..db import execute, query
from ..security import hash_password, hash_token
from ..services.audit import audit

bp = Blueprint("setup", __name__)


def setup_needed() -> bool:
    return query("SELECT id FROM users LIMIT 1", one=True) is None


# ---------------------------------------------------------------------------
# First-run wizard
# ---------------------------------------------------------------------------
@bp.route("/setup", methods=["GET", "POST"])
def first_run():
    if not setup_needed():
        return redirect(url_for("admin.login"))
    form = request.form
    if request.method == "POST":
        errors = []
        site_name = form.get("site_name", "").strip()
        username = form.get("username", "").strip()
        password = form.get("password", "")
        if not site_name:
            errors.append("Please give the name of your building or site")
        if len(username) < 3:
            errors.append("The username must have at least 3 characters")
        if len(password) < 8:
            errors.append("The password must have at least 8 characters")
        if password != form.get("password2", ""):
            errors.append("The two passwords are different")
        if not errors:
            ensure_defaults()
            site = query("SELECT id FROM sites WHERE name = ?", (site_name,), one=True)
            if site is None:
                execute("INSERT INTO sites (name, timezone) VALUES (?, ?)",
                        (site_name, form.get("timezone") or "Europe/Paris"))
            uid = execute("INSERT INTO users (username, password_hash, role) VALUES (?, ?, 'ADMIN')",
                          (username, hash_password(password)))
            audit(f"user:{username}", "FIRST_RUN_SETUP", "user", uid, {"site": site_name})
            session.clear()
            session["user_id"] = uid
            flash("Welcome! Add your employees, then open the punch terminal.", "ok")
            return redirect(url_for("admin.employees"))
        for e in errors:
            flash(e, "error")
    return render_template("setup.html", form=form)


# ---------------------------------------------------------------------------
# Desktop edition: kiosk of this computer
# ---------------------------------------------------------------------------
def _state_file():
    path = current_app.config.get("DESKTOP_STATE_FILE")
    return Path(path) if path else None


def _local_only():
    if not current_app.config.get("DESKTOP_MODE") or request.remote_addr not in ("127.0.0.1", "::1"):
        abort(404)


@bp.get("/desktop/kiosk")
def desktop_kiosk():
    _local_only()
    if setup_needed():
        return redirect(url_for("setup.first_run"))
    state_file = _state_file()
    state = {}
    if state_file and state_file.exists():
        state = json.loads(state_file.read_text(encoding="utf-8"))
    token = state.get("kiosk_token")
    valid = token and query("SELECT id FROM kiosks WHERE token_hash = ? AND active = 1",
                            (hash_token(token),), one=True)
    if not valid:
        site = query("SELECT id FROM sites WHERE active = 1 ORDER BY id LIMIT 1", one=True)
        if site is None:
            return redirect(url_for("setup.first_run"))
        _, token = create_kiosk("This computer", site["id"])
        state["kiosk_token"] = token
        if state_file:
            state_file.write_text(json.dumps(state), encoding="utf-8")
    return redirect(url_for("kiosk.kiosk_page", token=token))


@bp.get("/desktop/home")
def desktop_home():
    _local_only()
    return redirect(url_for("setup.first_run") if setup_needed() else url_for("admin.dashboard"))
