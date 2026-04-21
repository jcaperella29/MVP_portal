# app/main.py
import os
from pathlib import Path
from datetime import datetime

from flask import (
    Blueprint, request, jsonify, render_template, redirect,
    url_for, flash, abort, send_from_directory, current_app
)
from flask_login import login_required, current_user
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash
from sqlalchemy import inspect

from .extensions import db, send_email
from .models import GoalEntry, Experiment, User

# PDF (ReportLab)
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.units import inch
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer

main_bp = Blueprint("main", __name__)

# -----------------------------
# Storage helpers / config
# -----------------------------
BASE_UPLOAD = Path(os.getenv("UPLOAD_DIR", "uploads")).resolve()
ALLOWED = {".pdf", ".doc", ".docx", ".txt", ".png", ".jpg", ".jpeg", ".csv"}

def personal_dir(user_id) -> Path:
    p = BASE_UPLOAD / "personal" / str(user_id)
    p.mkdir(parents=True, exist_ok=True)
    return p

def general_dir() -> Path:
    p = BASE_UPLOAD / "general"
    p.mkdir(parents=True, exist_ok=True)
    return p

def _safe_child(base: Path, candidate: Path) -> Path:
    """Resolve candidate under base and ensure it doesn't escape base (no path traversal)."""
    base = base.resolve()
    cand = (base / candidate).resolve()
    if not str(cand).startswith(str(base)):
        raise ValueError("Invalid path")
    return cand

def _list_files(p: Path):
    p.mkdir(parents=True, exist_ok=True)
    class F:  # tiny object for Jinja readability
        def __init__(self, name): self.name = name
    return [F(x.name) for x in sorted(p.iterdir()) if x.is_file()]

def _list_dir(base: Path, subpath: str = ""):
    p = _safe_child(base, Path(subpath))
    p.mkdir(parents=True, exist_ok=True)
    dirs, files = [], []
    for entry in sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
        if entry.is_dir():
            dirs.append(entry.name)
        elif entry.is_file():
            files.append(entry.name)
    return p, dirs, files

# -----------------------------
# Basic nav
# -----------------------------
@main_bp.route("/")
def index():
    return redirect(url_for("main.dashboard"))

# -----------------------------
# Dashboard (cumulative, DB-backed)
# -----------------------------
@main_bp.route("/dashboard", methods=["GET"])
@login_required
def dashboard():
    exp = Experiment.query.filter_by(user_id=current_user.id).first()
    entries = (GoalEntry.query
               .filter_by(user_id=current_user.id)
               .order_by(GoalEntry.created_at.desc())
               .limit(200)
               .all())
    return render_template(
        "dashboard.html",
        entries=entries,
        experiment_title=(exp.title if exp else "")
    )

@main_bp.route("/api/goal_entries", methods=["POST"])
@login_required
def api_add_goal_entry():
    text = (request.form.get("text") or "").strip()
    if not text:
        return jsonify({"ok": False, "error": "Empty entry"}), 400
    entry = GoalEntry(user_id=current_user.id, text=text)
    db.session.add(entry)
    db.session.commit()
    return jsonify({
        "ok": True,
        "id": entry.id,
        "text": entry.text,
        "created_at": entry.created_at.strftime("%Y-%m-%d %H:%M:%S")
    })

@main_bp.route("/api/experiment", methods=["POST"])
@login_required
def api_save_experiment():
    title = (request.form.get("title") or "").strip()
    exp = Experiment.query.filter_by(user_id=current_user.id).first()
    if exp is None:
        exp = Experiment(user_id=current_user.id, title=title or "")
        db.session.add(exp)
    else:
        exp.title = title or ""
        exp.updated_at = datetime.utcnow()
    db.session.commit()
    return jsonify({"ok": True, "title": exp.title})

# -----------------------------
# Documents hub (root lists)
# -----------------------------
@main_bp.route("/documents")
@login_required
def documents():
    personal = _list_files(personal_dir(current_user.id))
    general = _list_files(general_dir())
    return render_template("documents.html",
                           personal_files=personal,
                           general_files=general)

# -----------------------------
# File browser (subfolders)
# -----------------------------
@main_bp.route("/documents/browse/<scope>")
@login_required
def browse_documents(scope):
    base = personal_dir(current_user.id) if scope == "personal" else general_dir()
    subpath = (request.args.get("path") or "").strip()
    try:
        current_dir, dirs, files = _list_dir(base, subpath)
    except Exception:
        abort(400)

    # breadcrumbs
    crumbs = [("Root", "")]
    accum = []
    for part in (Path(subpath).parts if subpath else []):
        accum.append(part)
        crumbs.append((part, "/".join(accum)))

    return render_template(
        "browser.html",
        scope=scope,
        subpath=subpath,
        breadcrumbs=crumbs,
        dirs=dirs,
        files=files
    )

# -----------------------------
# Upload / Download / Delete
# -----------------------------
@main_bp.route("/documents/upload/<scope>", methods=["POST"])
@login_required
def upload_document(scope):
    if scope not in {"personal", "general"}:
        abort(400)

    base = personal_dir(current_user.id) if scope == "personal" else general_dir()
    subpath = (request.form.get("path") or "").strip()
    try:
        target_dir = _safe_child(base, Path(subpath))
    except Exception:
        flash("Invalid folder path.", "danger")
        return redirect(url_for("main.documents"))

    files = request.files.getlist("files")
    saved = 0
    for fs in files:
        if not fs or fs.filename == "":
            continue
        name = secure_filename(fs.filename)
        ext = os.path.splitext(name)[1].lower()
        if ext not in ALLOWED:
            flash(f"Skipped unsupported type: {name}", "warning")
            continue

        target_dir.mkdir(parents=True, exist_ok=True)
        dst = (target_dir / name).resolve()

        if not str(dst).startswith(str(base.resolve())):
            flash("Blocked suspicious path.", "danger")
            continue

        fs.save(str(dst))
        saved += 1

    if saved:
        where = f"{scope}{('/' + subpath) if subpath else ''}"
        flash(f"Uploaded {saved} file(s) to {where}.", "success")

    if request.referrer and "/documents/browse/" in request.referrer:
        return redirect(request.referrer)
    return redirect(url_for("main.documents"))

@main_bp.route("/documents/download/<scope>/<path:filename>")
@login_required
def download_document(scope, filename):
    base = personal_dir(current_user.id) if scope == "personal" else general_dir()
    try:
        resolved = _safe_child(base, Path(filename))
    except Exception:
        abort(400)
    return send_from_directory(
        directory=str(resolved.parent),
        path=resolved.name,
        as_attachment=True
    )

@main_bp.route("/documents/delete/<scope>/<path:filename>")
@login_required
def delete_document(scope, filename):
    if scope != "personal":
        flash("Deleting from shared library is disabled for now.", "warning")
        return redirect(url_for("main.documents"))
    base = personal_dir(current_user.id)
    try:
        target = _safe_child(base, Path(filename))
        target.unlink()
        flash("Deleted.", "info")
    except FileNotFoundError:
        flash("File not found.", "warning")
    except Exception:
        flash("Invalid path.", "danger")

    if request.referrer and "/documents/browse/" in request.referrer:
        return redirect(request.referrer)
    return redirect(url_for("main.documents"))

# -----------------------------
# Quick Form (capture → PDF)
# -----------------------------
@main_bp.route("/forms/quick", methods=["GET", "POST"])
@login_required
def quick_form():
    if request.method == "POST":
        title   = (request.form.get("title") or "").strip()
        goal    = (request.form.get("goal") or "").strip()
        plan    = (request.form.get("plan") or "").strip()
        action  = (request.form.get("action") or "").strip()
        resp    = (request.form.get("response") or "").strip()
        learn   = (request.form.get("learn") or "").strip()

        if not title:
            flash("Please give this entry a title.", "warning")
            return render_template("quick_form.html")

        ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        safe_title = secure_filename(title) or "entry"
        target_dir = personal_dir(current_user.id) / "forms"
        target_dir.mkdir(parents=True, exist_ok=True)
        pdf_path = target_dir / f"{ts}__{safe_title}.pdf"

        styles = getSampleStyleSheet()
        H1 = styles["Heading1"]; H1.spaceAfter = 12
        H2 = styles["Heading2"]; H2.spaceBefore = 10; H2.spaceAfter = 4
        Body = styles["BodyText"]; Body.spaceAfter = 8

        story = [Paragraph(title, H1)]

        def add_section(label, txt):
            story.append(Paragraph(label, H2))
            story.append(Paragraph((txt if txt else "-").replace("\n", "<br/>"), Body))
            story.append(Spacer(1, 0.05*inch))

        add_section("Goal", goal)
        add_section("Plan", plan)
        add_section("Action / What I did", action)
        add_section("Response I got", resp)
        add_section("What I learned & will do next", learn)

        doc = SimpleDocTemplate(
            str(pdf_path),
            pagesize=LETTER,
            leftMargin=0.85*inch, rightMargin=0.85*inch,
            topMargin=0.85*inch, bottomMargin=0.85*inch
        )
        doc.build(story)

        flash("Saved to your Personal library as PDF (under forms/).", "success")
        return redirect(url_for("main.documents"))

    return render_template("quick_form.html")

# -----------------------------
# Calendar / Chat / Contact / Goals / Help
# -----------------------------
@main_bp.route("/calendar")
@login_required
def calendar():
    return render_template("calendar.html")

@main_bp.route("/chat")
@login_required
def chat():
    return render_template("chat.html")

@main_bp.route("/contact", methods=["GET", "POST"])
@login_required
def contact():
    contact_enabled = bool(current_app.config.get("CONTACT_FORM_ENABLED", True))

    john_email  = "jcaperella@gmail.com"
    mitch_email = "mitch.dickey@theinflectionpoint.com"

    if request.method == "POST" and contact_enabled:
        from_email = (request.form.get("from_email") or "").strip()
        subject    = (request.form.get("subject") or "").strip()
        message    = (request.form.get("message") or "").strip()

        if not (from_email and subject and message):
            flash("All fields are required.", "warning")
            return render_template(
                "contact.html",
                contact_form_enabled=contact_enabled,
                john_email=john_email,
                mitch_email=mitch_email,
                from_email=from_email,
                subject=subject,
                message=message,
            )

        recipients_cfg = current_app.config.get(
            "CONTACT_RECIPIENTS",
            f"{john_email},{mitch_email}"
        )
        recipients = [r.strip() for r in recipients_cfg.replace(";", ",").split(",") if r.strip()]

        body = f"From: {from_email}\n\n{message}\n"

        try:
            send_email(
                subject=subject,
                recipients=recipients,
                body=body,
                html=None,
                reply_to=[from_email],
            )
            flash("Message sent.", "success")
            return redirect(url_for("main.contact"))
        except Exception:
            current_app.logger.exception("contact send failed")
            flash("Could not send your message. Please try again.", "danger")

    return render_template(
        "contact.html",
        contact_form_enabled=contact_enabled,
        john_email=john_email,
        mitch_email=mitch_email,
    )

@main_bp.get("/__admin/initdb")
def __admin_initdb():
    token = request.args.get("token")
    if token != current_app.config.get("ADMIN_INIT_TOKEN"):
        return "forbidden", 403
    insp = inspect(db.engine)
    # For your schema name: your tables are singular "user" etc.
    if "user" not in insp.get_table_names():
        db.create_all()
        return "db initialized (tables created)", 200
    return "db already initialized", 200

@main_bp.get("/__admin/seed_user")
def __admin_seed_user():
    token = request.args.get("token")
    if token != current_app.config.get("ADMIN_INIT_TOKEN"):
        return "forbidden", 403

    email = (request.args.get("email") or "").strip().lower()
    password = request.args.get("password", "ChangeMe123!")
    name = request.args.get("name", "")

    if not email:
        return "missing email", 400

    u = User.query.filter_by(email=email).first()
    if u:
        return f"user already exists: {email}", 200

    u = User(email=email, name=name, password_hash=generate_password_hash(password))
    db.session.add(u)
    db.session.commit()
    return f"seeded {email}", 200

@main_bp.route("/goals")
@login_required
def goals():
    return render_template("goals.html")

@main_bp.route("/help")
@login_required
def readme():
    return render_template("Readme.html")


