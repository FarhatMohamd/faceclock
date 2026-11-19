"""
Security helpers
================

* Secret PIN hashing, verification and brute-force lockout
* Kiosk device tokens
* Back-office authentication, role checks and CSRF protection
"""

import hashlib
import hmac
import secrets
from datetime import timedelta
from functools import wraps

from flask import abort, current_app, g, redirect, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from .db import execute, from_iso, now_iso, query, to_iso, utcnow


class PinError(Exception):
    """Raised when a PIN check fails (wrong PIN, locked account, unknown code)."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# Secret PIN
# ---------------------------------------------------------------------------
def hash_pin(pin: str) -> str:
    return generate_password_hash(pin)


def validate_pin_format(pin: str):
    cfg = current_app.config
    if not pin or not pin.isdigit() or not (cfg["PIN_MIN_LENGTH"] <= len(pin) <= 8):
        raise PinError("PIN_FORMAT", f"PIN must be {cfg['PIN_MIN_LENGTH']} to 8 digits")
    if len(set(pin)) == 1 or pin in "0123456789" or pin in "9876543210":
        raise PinError("PIN_WEAK", "PIN is too easy to guess")


def verify_employee_pin(employee_code: str, pin: str):
    """Return the employee row if the PIN is correct, otherwise raise PinError.

    After ``PIN_MAX_ATTEMPTS`` failures the employee is locked for
    ``PIN_LOCK_MINUTES`` minutes (protection against brute force at a kiosk).
    """
    cfg = current_app.config
    emp = query(
        "SELECT * FROM employees WHERE employee_code = ? AND active = 1",
        (employee_code.strip(),), one=True,
    )
    if emp is None:
        # Same message as a wrong PIN: do not reveal which codes exist.
        check_password_hash(hash_pin("0000"), pin or "")   # constant-ish time
        raise PinError("BAD_CREDENTIALS", "Unknown employee code or wrong PIN")

    if emp["pin_locked_until"] and from_iso(emp["pin_locked_until"]) > utcnow():
        raise PinError("LOCKED", "Too many failed attempts, try again later")

    if check_password_hash(emp["pin_hash"], pin or ""):
        if emp["pin_failed_attempts"]:
            execute("UPDATE employees SET pin_failed_attempts = 0, pin_locked_until = NULL "
                    "WHERE id = ?", (emp["id"],))
        return emp

    attempts = emp["pin_failed_attempts"] + 1
    locked_until = None
    if attempts >= cfg["PIN_MAX_ATTEMPTS"]:
        locked_until = to_iso(utcnow() + timedelta(minutes=cfg["PIN_LOCK_MINUTES"]))
        attempts = 0
        from .services.audit import audit
        audit("system", "PIN_LOCKED", "employee", emp["id"],
              {"minutes": cfg["PIN_LOCK_MINUTES"]})
    execute("UPDATE employees SET pin_failed_attempts = ?, pin_locked_until = ? WHERE id = ?",
            (attempts, locked_until, emp["id"]))
    raise PinError("BAD_CREDENTIALS", "Unknown employee code or wrong PIN")


# ---------------------------------------------------------------------------
# Kiosk tokens
# ---------------------------------------------------------------------------
def new_kiosk_token() -> str:
    return "kio_" + secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def kiosk_required(view):
    """Authenticate a kiosk with the ``X-Kiosk-Token`` header."""

    @wraps(view)
    def wrapper(*args, **kwargs):
        token = request.headers.get("X-Kiosk-Token", "")
        kiosk = None
        if token:
            kiosk = query(
                "SELECT k.*, s.name AS site_name, s.timezone AS site_tz "
                "FROM kiosks k JOIN sites s ON s.id = k.site_id "
                "WHERE k.token_hash = ? AND k.active = 1",
                (hash_token(token),), one=True,
            )
        if kiosk is None:
            return {"ok": False, "error": "KIOSK_UNAUTHORIZED",
                    "message": "Unknown or disabled kiosk"}, 401
        g.kiosk = kiosk
        execute("UPDATE kiosks SET last_seen = ? WHERE id = ?", (now_iso(), kiosk["id"]))
        return view(*args, **kwargs)

    return wrapper


# ---------------------------------------------------------------------------
# Back-office users
# ---------------------------------------------------------------------------
ROLES = ("ADMIN", "HR", "MANAGER")


def hash_password(password: str) -> str:
    return generate_password_hash(password)


def authenticate_user(username: str, password: str):
    user = query("SELECT * FROM users WHERE username = ? AND active = 1",
                 (username.strip(),), one=True)
    if user and check_password_hash(user["password_hash"], password):
        return user
    return None


def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    if "user" not in g:
        g.user = query("SELECT * FROM users WHERE id = ? AND active = 1", (uid,), one=True)
    return g.user


def login_required(*roles):
    """Restrict a view to logged-in users, optionally to some roles."""

    def decorator(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            user = current_user()
            if user is None:
                return redirect(url_for("admin.login", next=request.path))
            if roles and user["role"] not in roles:
                abort(403)
            return view(*args, **kwargs)

        return wrapper

    return decorator


def visible_department_ids(user):
    """Departments a user may see. ``None`` means "all" (ADMIN / HR).

    A MANAGER sees their own department and every sub-department (a director
    sees the whole direction, a team leader only their team).
    """
    if user["role"] in ("ADMIN", "HR"):
        return None
    if not user["department_id"]:
        return set()
    rows = query(
        """
        WITH RECURSIVE tree(id) AS (
            SELECT ?
            UNION ALL
            SELECT d.id FROM departments d JOIN tree t ON d.parent_id = t.id
        )
        SELECT id FROM tree
        """,
        (user["department_id"],),
    )
    return {r["id"] for r in rows}


# ---------------------------------------------------------------------------
# CSRF protection for HTML forms
# ---------------------------------------------------------------------------
def csrf_token() -> str:
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_urlsafe(24)
    return session["_csrf"]


def check_csrf():
    if not current_app.config.get("CSRF_ENABLED", True):
        return
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        sent = request.form.get("_csrf") or request.headers.get("X-CSRF-Token", "")
        if not hmac.compare_digest(sent, session.get("_csrf", "")):
            abort(400, "Invalid CSRF token")
