"""
FaceClock - face-recognition time & attendance for multi-site companies.

Application factory.
"""

import logging

from flask import Flask, redirect, url_for

from . import db
from .config import Config
from .face import FaceGallery, create_engine
from .security import check_csrf, csrf_token, current_user

__version__ = "1.0.0"


def create_app(config_object=Config, **overrides):
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_object(config_object)
    app.config.update(overrides)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    # --- Database -----------------------------------------------------------
    db.init_app(app)
    with app.app_context():
        conn = db.connect(app.config["DATABASE"])
        db.init_schema(conn)
        conn.close()

    # --- Face engine (falls back to PIN-only mode if unavailable) -----------
    app.extensions["faceclock"] = {"engine": create_engine(app.config), "gallery": FaceGallery()}

    # --- Blueprints ---------------------------------------------------------
    from .api.kiosk import bp as kiosk_api
    from .views.admin import bp as admin_bp
    from .views.kiosk import bp as kiosk_bp
    from .views.portal import bp as portal_bp

    app.register_blueprint(kiosk_api)
    app.register_blueprint(kiosk_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(portal_bp)

    # --- CLI commands -------------------------------------------------------
    from .cli import register_cli
    register_cli(app)

    # --- Security -----------------------------------------------------------
    @app.before_request
    def _csrf():
        from flask import request
        if request.blueprint in ("admin", "portal"):
            check_csrf()

    @app.after_request
    def _headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        resp.headers.setdefault("Permissions-Policy", "camera=(self), microphone=()")
        return resp

    # --- Template helpers ---------------------------------------------------
    from .services.timesheet import fmt_minutes

    @app.context_processor
    def _inject():
        return {"csrf_token": csrf_token, "current_user": current_user,
                "fmt_minutes": fmt_minutes, "app_version": __version__}

    @app.get("/")
    def index():
        return redirect(url_for("admin.dashboard"))

    @app.get("/health")
    def health():
        eng = app.extensions["faceclock"]["engine"]
        return {"status": "ok", "version": __version__, "face_engine": eng.name,
                "biometrics": bool(eng.available)}

    return app
