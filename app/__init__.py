# app/__init__.py
import os
import sys
import logging

from flask import Flask, current_app
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.exceptions import HTTPException

from sqlalchemy import event, inspect
from sqlalchemy.engine import Engine

from .extensions import db, login_manager


def create_app():
    app = Flask(__name__)

    # ---- Basic config (env overrides) ----
    app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "dev")
    app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv("DATABASE_URL", "sqlite:///app.db")
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    # Optional admin token for one-time init/seed routes
    app.config["ADMIN_INIT_TOKEN"] = os.getenv("ADMIN_INIT_TOKEN")

    # ---- Logging to stdout (App Runner -> CloudWatch) ----
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(logging.INFO)
    app.logger.handlers = [handler]
    app.logger.setLevel(logging.INFO)
    app.config["PROPAGATE_EXCEPTIONS"] = True

    # ---- Respect reverse proxy headers ----
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    # ---- Init extensions ----
    db.init_app(app)
    login_manager.init_app(app)

    # Enable SQLite FK cascades when SQLite is in use
    @event.listens_for(Engine, "connect")
    def _set_sqlite_pragma(dbapi_conn, _):
        try:
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()
        except Exception:
            pass

    # Import models so SQLAlchemy knows them
    from . import models  # noqa: F401
    from .models import User  # noqa: F401

    # ---- DB init (dev-safe) ----
    with app.app_context():
        try:
            app.logger.info(f"DB URL -> {db.engine.url!s}")
        except Exception:
            app.logger.exception("Could not introspect engine URL")

        try:
            db.create_all()
            app.logger.info("db.create_all() completed")
        except Exception:
            app.logger.exception("db.create_all() failed")

        try:
            insp = inspect(db.engine)
            app.logger.info(f"Present tables: {insp.get_table_names()}")
        except Exception:
            app.logger.exception("Could not list tables")

    # ---- Blueprints ----
    from .auth import auth_bp
    from .main import main_bp
    app.register_blueprint(auth_bp)
    app.register_blueprint(main_bp)

    # ---- Health/debug ----
    @app.get("/healthz")
    def healthz():
        return "ok", 200

    @app.get("/_debug/ping")
    def _root_ping():
        return "pong", 200

    # ---- Global error handler ----
    def _unhandled(e):
        if isinstance(e, HTTPException):
            return e
        current_app.logger.exception("Unhandled exception")
        return ("Internal Server Error", 500)

    app.register_error_handler(Exception, _unhandled)

    return app

