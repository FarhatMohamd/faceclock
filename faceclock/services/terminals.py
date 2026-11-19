"""
Punch terminals started from the application
============================================

"Start" in the back-office (or in the desktop window) turns the current
screen into the punch terminal of a building, without copying any token:

* the first time, a kiosk is registered for that building and its secret
  token is remembered by this device (desktop edition: settings file;
  browser: signed, HTTP-only cookie);
* the next times, the same kiosk is reused.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from flask import current_app
from itsdangerous import BadSignature, URLSafeSerializer

from ..cli import create_kiosk
from ..db import query
from ..security import hash_token

COOKIE = "fc_terminals"
COOKIE_MAX_AGE = 5 * 365 * 24 * 3600


def active_sites():
    return query("SELECT * FROM sites WHERE active = 1 ORDER BY name")


def _token_is_valid(token, site_id):
    if not token:
        return False
    row = query("SELECT site_id FROM kiosks WHERE token_hash = ? AND active = 1",
                (hash_token(token),), one=True)
    return row is not None and row["site_id"] == site_id


def token_for_site(site_id: int, store: dict, name: str) -> str:
    """Reuse the terminal this device already has for the site, or register one.

    ``store`` maps str(site_id) -> token and is updated in place.
    """
    token = store.get(str(site_id))
    if not _token_is_valid(token, site_id):
        _, token = create_kiosk(name, site_id)
        store[str(site_id)] = token
    return token


# ---------------------------------------------------------------------------
# Device stores
# ---------------------------------------------------------------------------
def _serializer():
    return URLSafeSerializer(current_app.config["SECRET_KEY"], salt="terminals")


def read_cookie_store(request) -> dict:
    raw = request.cookies.get(COOKIE)
    if not raw:
        return {}
    try:
        data = _serializer().loads(raw)
        return data if isinstance(data, dict) else {}
    except BadSignature:
        return {}


def write_cookie_store(response, store: dict):
    response.set_cookie(COOKIE, _serializer().dumps(store), max_age=COOKIE_MAX_AGE,
                        httponly=True, samesite="Lax",
                        secure=current_app.config.get("SESSION_COOKIE_SECURE", False))


def read_desktop_store() -> dict:
    path = current_app.config.get("DESKTOP_STATE_FILE")
    if not path or not Path(path).exists():
        return {}
    state = json.loads(Path(path).read_text(encoding="utf-8"))
    store = dict(state.get("kiosk_tokens", {}))
    if state.get("kiosk_token") and not store:             # settings of version 1.1
        row = query("SELECT site_id FROM kiosks WHERE token_hash = ?",
                    (hash_token(state["kiosk_token"]),), one=True)
        if row:
            store[str(row["site_id"])] = state["kiosk_token"]
    return store


def write_desktop_store(store: dict):
    path = current_app.config.get("DESKTOP_STATE_FILE")
    if not path:
        return
    p = Path(path)
    state = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    state["kiosk_tokens"] = store
    p.write_text(json.dumps(state, indent=2), encoding="utf-8")


def default_name(site_name: str, desktop: bool) -> str:
    where = "This computer" if desktop else "Browser terminal"
    return f"{where} - {site_name} ({date.today().isoformat()})"
