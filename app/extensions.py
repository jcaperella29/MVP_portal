# app/extensions.py
import os
import boto3
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
from flask import current_app

db = SQLAlchemy()
login_manager = LoginManager()
login_manager.login_view = "auth.login"

def _from_email() -> str:
    """
    Return bare email for SES Source. Accepts "Name <email@domain>" or just email.
    """
    src = current_app.config.get("MAIL_DEFAULT_SENDER", "Portal <no-reply@theinflectionpoint.com>")
    if "<" in src and ">" in src:
        return src[src.find("<")+1 : src.find(">")]
    return src
import os, boto3
from flask import current_app

def send_email(subject: str, recipients: list[str], body: str,
               html: str | None = None, reply_to: list[str] | None = None):
    region = os.getenv("SES_REGION") or os.getenv("AWS_REGION") or "us-east-2"
    ses = boto3.client("ses", region_name=region)

    message = {
        "Subject": {"Data": subject},
        "Body": {
            "Text": {"Data": body},
            "Html": {"Data": html or f"<pre>{body}</pre>"},
        },
    }

    kwargs = dict(
        Source=_from_email(),  # your verified sender
        Destination={"ToAddresses": recipients},
        Message=message,
    )
    if reply_to:
        kwargs["ReplyToAddresses"] = reply_to

    return ses.send_email(**kwargs)
