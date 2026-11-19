"""
First-run setup and desktop helpers
===================================

* /setup         - shown only while no back-office user exists: creates the
                   first building and the administrator account, then logs in.
                   Replaces the command line for non-technical users.
* /terminal      - "Start" button: opens the punch terminal of a building on
                   this screen; the kiosk is registered automatically the
                   first time and reused afterwards.
"""

from flask import (Blueprint, abort, current_app, flash, redirect, render_template, request,
                   session, url_for)

from ..cli import ensure_defaults
from ..db import execute, query
from ..security import hash_password
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
# "Start": open the punch terminal on this screen, no token to copy
# ---------------------------------------------------------------------------
def _is_local_desktop():
    return bool(current_app.config.get("DESKTOP_MODE")) and request.remote_addr in ("127.0.0.1", "::1")


def _local_only():
    if not _is_local_desktop():
        abort(404)


@bp.get("/terminal")
def start_terminal():
    """Turn this screen into the punch terminal of a building.

    Desktop edition: allowed directly from this computer. Browser: requires a
    back-office login (ADMIN, HR or MANAGER), the terminal is then remembered
    by this browser.
    """
    from ..security import current_user
    from ..services import terminals

    desktop = _is_local_desktop()
    if setup_needed():
        return redirect(url_for("setup.first_run"))
    if not desktop and current_user() is None:
        return redirect(url_for("admin.login", next=url_for("setup.start_terminal")))

    sites = terminals.active_sites()
    if not sites:
        flash("Create a building first (Organization).", "error")
        return redirect(url_for("admin.organization"))
    store = terminals.read_desktop_store() if desktop else terminals.read_cookie_store(request)
    site = None
    if request.args.get("site_id", "").isdigit():
        site = next((s for s in sites if s["id"] == int(request.args["site_id"])), None)
    elif len(sites) == 1:
        site = sites[0]
    elif not request.args.get("choose") and len(store) == 1:
        # this device already runs the terminal of one building: open it directly
        site = next((s for s in sites if str(s["id"]) in store), None)
    if site is None:
        return render_template("terminal_choose.html", sites=sites)

    token = terminals.token_for_site(site["id"], store, terminals.default_name(site["name"], desktop))
    response = redirect(url_for("kiosk.kiosk_page", token=token))
    if desktop:
        terminals.write_desktop_store(store)
    else:
        terminals.write_cookie_store(response, store)
    return response


@bp.get("/desktop/kiosk")
def desktop_kiosk():
    """Kept for the control window of version 1.1."""
    _local_only()
    return redirect(url_for("setup.start_terminal"))


@bp.get("/desktop/home")
def desktop_home():
    _local_only()
    return redirect(url_for("setup.first_run") if setup_needed() else url_for("admin.dashboard"))
