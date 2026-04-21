import os
from flask import Blueprint, render_template, redirect, url_for, request, flash, current_app
from flask_login import login_user, logout_user, login_required
from werkzeug.security import generate_password_hash, check_password_hash
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from botocore.exceptions import ClientError
from sqlalchemy.exc import OperationalError

from .extensions import db, send_email
from .models import User

auth_bp = Blueprint('auth', __name__, url_prefix='/auth')


# --------------------------
# Helpers
# --------------------------
def _serializer():
    return URLSafeTimedSerializer(
        current_app.config["SECRET_KEY"],
        salt=current_app.config.get("SECURITY_PASSWORD_SALT", "password-reset"),
    )


def send_reset_email(user):
    current_app.logger.info("Preparing password reset email for %s", user.email)

    token = _serializer().dumps(user.email)
    reset_url = url_for("auth.reset_password", token=token, _external=True)

    subject = "Reset your Portal password"
    body = f"""Hi {user.name or 'there'},

We received a request to reset your password.
Click the link below to choose a new one (valid for 1 hour):

{reset_url}

If you didn't request this, you can ignore this email.
"""

    html = None
    try:
        html = render_template("emails/reset_password.html", user=user, reset_url=reset_url)
    except Exception:
        current_app.logger.warning("Could not render HTML reset template for %s; falling back to text email", user.email)

    try:
        result = send_email(subject, [user.email], body=body, html=html)
        current_app.logger.info("Password reset email sent to %s; result=%r", user.email, result)
        return True, None

    except ClientError as ce:
        code = ce.response.get("Error", {}).get("Code")
        msg = ce.response.get("Error", {}).get("Message")
        current_app.logger.warning(
            "SES ClientError sending reset to %s: %s - %s",
            user.email,
            code,
            msg,
        )

        fallback = os.getenv("DEBUG_TO")
        if fallback:
            try:
                send_email(subject, [fallback], body=body, html=html)
                current_app.logger.info("Fallback reset email sent to DEBUG_TO=%s", fallback)
            except Exception:
                current_app.logger.exception("Fallback DEBUG_TO send failed")

        return True, {"code": code, "message": msg}

    except Exception as e:
        current_app.logger.exception("Unexpected error sending reset email to %s", user.email)
        return False, {"code": "Unexpected", "message": str(e)}


# --------------------------
# Auth routes
# --------------------------
@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""

        user = None
        try:
            user = User.query.filter_by(email=email).first()
        except OperationalError:
            current_app.logger.exception("DB not initialized on login")
            flash("Internal error, please try again shortly.", "danger")
            return render_template("login.html")

        if user and check_password_hash(user.password_hash, password):
            login_user(user)
            current_app.logger.info("Successful login for %s", email)
            return redirect(url_for("main.dashboard"))

        current_app.logger.info("Failed login attempt for %s", email)
        flash("Invalid email or password", "danger")

    return render_template("login.html")


@auth_bp.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        name = request.form.get("name") or ""
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""

        if not email or not password:
            flash("Email and password are required.", "danger")
            return render_template("register.html")

        try:
            if User.query.filter_by(email=email).first():
                current_app.logger.info("Registration attempted for existing email %s", email)
                flash("Email already registered.", "warning")
                return render_template("register.html")

            user = User(email=email, name=name, password_hash=generate_password_hash(password))
            db.session.add(user)
            db.session.commit()

            current_app.logger.info("Registered new user %s", email)

        except OperationalError:
            current_app.logger.exception("DB not initialized on register")
            flash("Internal error, please try again shortly.", "danger")
            return render_template("register.html")

        flash("Registered! Please log in.", "success")
        return redirect(url_for("auth.login"))

    return render_template("register.html")


@auth_bp.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("auth.login"))


@auth_bp.route("/forgot", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        current_app.logger.info("Forgot password requested for %s", email)

        user = None
        try:
            user = User.query.filter_by(email=email).first()
            current_app.logger.info("Forgot password lookup for %s -> found=%s", email, bool(user))
        except OperationalError:
            current_app.logger.exception("DB not initialized in forgot flow")

        if user:
            current_app.logger.info("Calling send_reset_email for %s", email)
            sent, err = send_reset_email(user)
            current_app.logger.info("send_reset_email returned for %s -> sent=%s err=%r", email, sent, err)

            if err:
                current_app.logger.warning("Reset email issue for %s: %r", email, err)
        else:
            current_app.logger.info("No user found for forgot password email %s", email)

        flash("If that email exists, we sent a reset link.", "info")
        return redirect(url_for("auth.login"))

    return render_template("forgot_password.html")


@auth_bp.route("/reset/<token>", methods=["GET", "POST"])
def reset_password(token):
    try:
        email = _serializer().loads(token, max_age=3600)
    except SignatureExpired:
        flash("Reset link expired. Please request a new one.", "warning")
        return redirect(url_for("auth.forgot_password"))
    except BadSignature:
        flash("Invalid reset link.", "danger")
        return redirect(url_for("auth.forgot_password"))

    user = User.query.filter_by(email=email).first_or_404()

    if request.method == "POST":
        pw1 = request.form.get("password") or ""
        pw2 = request.form.get("confirm") or ""

        if len(pw1) < 8:
            flash("Password must be at least 8 characters.", "danger")
        elif pw1 != pw2:
            flash("Passwords do not match.", "danger")
        else:
            user.password_hash = generate_password_hash(pw1)
            db.session.commit()
            current_app.logger.info("Password updated for %s", email)
            flash("Password updated. You can log in now.", "success")
            return redirect(url_for("auth.login"))

    return render_template("reset_password.html", email=email)


# --- Debug helpers ---
@auth_bp.get("/_debug/ping")
def _debug_ping():
    return "auth pong", 200


@auth_bp.get("/_debug/send_test")
def send_test():
    to = os.getenv("DEBUG_TO", current_app.config.get("ADMIN_EMAIL", "YOUR_EMAIL@example.com"))
    current_app.logger.info("Running send_test endpoint; destination=%s", to)

    try:
        res = send_email(
            "SES API test",
            [to],
            body="Hello from App Runner via SES API.",
            html="<p>Hello from <b>SES API</b>.</p>",
        )
        src = current_app.config.get("MAIL_DEFAULT_SENDER")
        region = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION")
        current_app.logger.info("send_test succeeded; source=%s region=%s resp=%r", src, region, res)
        return f"OK: sent. source={src} region={region} resp={res}", 200

    except ClientError as ce:
        current_app.logger.exception("SES ClientError in send_test")
        return f"SES ClientError: {ce.response}", 500

    except Exception as e:
        current_app.logger.exception("SES Error in send_test")
        return f"SES Error: {e}", 500

