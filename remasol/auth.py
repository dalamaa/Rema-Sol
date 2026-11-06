from __future__ import annotations

from functools import wraps

from flask import current_app, redirect, render_template, session, url_for
from werkzeug.security import check_password_hash


def merchant_is_configured() -> bool:
    return bool(
        current_app.config.get("MERCHANT_USERNAME")
        and current_app.config.get("MERCHANT_PASSWORD_HASH")
    )


def merchant_authenticated() -> bool:
    return bool(session.get("merchant_authenticated"))


def merchant_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not merchant_is_configured():
            return render_template(
                "error.html",
                title="Merchant access is not configured",
                message="Set REMA_SOL_MERCHANT_PASSWORD_HASH before opening the merchant console.",
            ), 503
        if not merchant_authenticated():
            return redirect(url_for("main.merchant_login"))
        return view(*args, **kwargs)

    return wrapped


def verify_merchant_credentials(username: str, password: str) -> bool:
    if not merchant_is_configured() or username != current_app.config["MERCHANT_USERNAME"]:
        return False
    try:
        return check_password_hash(current_app.config["MERCHANT_PASSWORD_HASH"], password)
    except (ValueError, TypeError):
        return False
