from __future__ import annotations

import os
from pathlib import Path

from flask import Flask, render_template
from dotenv import load_dotenv
from werkzeug.middleware.proxy_fix import ProxyFix

from .config import Config
from .db import close_db, init_db
from .auth import merchant_authenticated
from .rate_limit import RateLimiter
from .routes import bp
from .services import PaymentEngine
from .solana_service import SolanaService


def create_app(test_config: dict | None = None) -> Flask:
    load_dotenv()
    project_dir = Path(__file__).resolve().parent.parent
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_object(Config)
    app.config.from_mapping(
        PORT=int(os.environ.get("PORT", "5007")),
        DATABASE=os.environ.get("REMA_SOL_DATABASE", str(project_dir / "remasol.sqlite3")),
        SECRET_KEY=os.environ.get("REMA_SOL_SECRET_KEY", "dev-only-change-me"),
        WALLET_ENCRYPTION_KEY=os.environ.get("REMA_SOL_WALLET_KEY", ""),
        SOLANA_RPC_URL=os.environ.get("SOLANA_RPC_URL", "https://api.devnet.solana.com"),
        RATE_LIMIT_ENABLED=os.environ.get("RATE_LIMIT_ENABLED", "1") == "1",
        CHECKOUT_EXPIRY_MINUTES=int(os.environ.get("CHECKOUT_EXPIRY_MINUTES", "60")),
        MAX_UNPAID_CHECKOUTS_PER_IP=int(os.environ.get("MAX_UNPAID_CHECKOUTS_PER_IP", "8")),
        REFUND_FEE_BUFFER_LAMPORTS=int(os.environ.get("REFUND_FEE_BUFFER_LAMPORTS", "10000")),
        MIN_CLAIM_ATTEMPTS_PER_MINUTE=int(os.environ.get("MIN_CLAIM_ATTEMPTS_PER_MINUTE", "12")),
        MERCHANT_USERNAME=os.environ.get("REMA_SOL_MERCHANT_USERNAME", "merchant"),
        MERCHANT_PASSWORD_HASH=os.environ.get("REMA_SOL_MERCHANT_PASSWORD_HASH", ""),
        SESSION_COOKIE_SECURE=os.environ.get("REMA_SOL_COOKIE_SECURE", "0") == "1",
        TRUST_PROXY=os.environ.get("REMA_SOL_TRUST_PROXY", "0") == "1",
        DEBUG=os.environ.get("FLASK_DEBUG", "0") == "1",
    )
    if test_config:
        app.config.update(test_config)

    if app.config.get("TRUST_PROXY"):
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    Path(app.instance_path).mkdir(parents=True, exist_ok=True)
    app.config["MAX_CONTENT_LENGTH"] = 32 * 1024

    init_db(app)
    close_db_registration = app.teardown_appcontext(close_db)
    del close_db_registration

    solana_service = app.config.get("SOLANA_SERVICE") or SolanaService(
        app.config["SOLANA_RPC_URL"]
    )
    app.extensions["solana"] = solana_service
    app.extensions["rate_limiter"] = RateLimiter(
        enabled=app.config.get("RATE_LIMIT_ENABLED", True)
    )
    app.extensions["engine"] = PaymentEngine(
        db_path=app.config["DATABASE"],
        secret_key=app.config["SECRET_KEY"],
        wallet_encryption_key=app.config.get("WALLET_ENCRYPTION_KEY", ""),
        solana=solana_service,
        config=app.config,
    )

    app.register_blueprint(bp)

    @app.context_processor
    def inject_globals():
        return {
            "devnet": True,
            "app_name": "RemaSol",
            "merchant_authenticated": merchant_authenticated(),
        }

    @app.errorhandler(413)
    def request_too_large(_error):
        return render_template(
            "error.html",
            title="Request too large",
            message="That request is larger than RemaSol accepts.",
        ), 413

    @app.errorhandler(404)
    def page_not_found(_error):
        return render_template(
            "error.html",
            title="Page not found",
            message="This payment room does not exist or has expired.",
        ), 404

    @app.errorhandler(400)
    def bad_request(_error):
        return render_template(
            "error.html",
            title="Request could not be processed",
            message="The form was missing required security or payment details. Refresh the page and try again.",
        ), 400

    @app.errorhandler(403)
    def forbidden(_error):
        return render_template(
            "error.html",
            title="Access not authorized",
            message="This page is private to the checkout session that created it. Use the original checkout or recovery link.",
        ), 403

    @app.errorhandler(429)
    def too_many_requests(_error):
        return render_template(
            "error.html",
            title="Too many requests",
            message="Please wait a moment before trying this again. No payment or refund was created by this request.",
        ), 429

    return app
