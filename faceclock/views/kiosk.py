"""Kiosk page (full-screen punch terminal)."""

from flask import Blueprint, render_template

bp = Blueprint("kiosk", __name__)


@bp.get("/kiosk")
def kiosk_page():
    # The kiosk token is read client-side (query string, then saved in localStorage).
    return render_template("kiosk.html")
