from __future__ import annotations

import secrets

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from .services import AuthorizationError, DomainError, sol_from_lamports
from .auth import (
    merchant_authenticated,
    merchant_is_configured,
    merchant_required,
    verify_merchant_credentials,
)


bp = Blueprint("main", __name__)


def engine():
    return current_app.extensions["engine"]


def limiter():
    return current_app.extensions["rate_limiter"]


def demo_service():
    return current_app.extensions["demo"]


def client_ip() -> str:
    # ProxyFix is enabled only when REMA_SOL_TRUST_PROXY=1. Never trust a raw
    # X-Forwarded-For header from an internet client for rate-limit identity.
    return request.remote_addr or "unknown"


def demo_browser_id() -> str:
    if "demo_browser_id" not in session:
        session["demo_browser_id"] = secrets.token_urlsafe(24)
    return session["demo_browser_id"]


def wallet_browser_id() -> str:
    if "wallet_browser_id" not in session:
        session["wallet_browser_id"] = secrets.token_urlsafe(24)
    return session["wallet_browser_id"]


def csrf_token() -> str:
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(32)
    return session["csrf_token"]


def csrf_ok() -> bool:
    return bool(session.get("csrf_token")) and secrets.compare_digest(
        session.get("csrf_token", ""), request.form.get("csrf_token", "")
    )


def template_context(**values):
    values.setdefault("csrf_token", csrf_token())
    return values


def explorer_url(signature: str | None) -> str | None:
    return f"https://explorer.solana.com/tx/{signature}?cluster=devnet" if signature else None


def render_domain_error(template: str, error: DomainError, **values):
    values["error"] = str(error)
    values.setdefault("error_code", error.code)
    return render_template(template, **template_context(**values)), 400


def wallet_json_request() -> bool:
    """Return whether the Test Wallet asked for its same-page response format."""
    return request.headers.get("X-Requested-With") == "fetch" or (
        request.accept_mimetypes.best == "application/json"
    )


def wallet_json_response(wallet: dict | None, *, error: str | None = None, status_code: int = 200):
    """Expose only the wallet fields the browser needs to render its result."""
    public_wallet = None
    if wallet:
        public_wallet = {
            "id": wallet.get("id"),
            "status": wallet.get("status"),
            "sender_address": wallet.get("sender_address"),
            "recipient_address": wallet.get("recipient_address"),
            "selected_sol": wallet.get("selected_sol"),
            "payment_signature": wallet.get("payment_signature"),
            "last_error": wallet.get("last_error"),
        }
    status = public_wallet.get("status") if public_wallet else None
    return jsonify(
        {
            "ok": error is None,
            "wallet": public_wallet,
            "status": status,
            "status_label": status_label(status) if status else None,
            "error": error,
            "explorer_url": explorer_url(public_wallet.get("payment_signature"))
            if public_wallet
            else None,
        }
    ), status_code


def wallet_maintenance_response(session_id: str, wants_json: bool):
    """Stop state-changing wallet work while preserving owner-scoped status."""
    state = demo_service().wallet_page_state(
        wallet_browser_id(), session_id, include_terminal=True
    )
    error = "Test Wallet operations are temporarily paused for maintenance."
    if wants_json:
        return wallet_json_response(state.get("wallet"), error=error, status_code=503)
    return render_template(
        "wallet.html", **state, error=error, **template_context()
    ), 503


def wallet_operation_is_paused(session_id: str) -> bool:
    """Keep maintenance global while permitting one explicitly selected canary."""
    if not current_app.config.get("WALLET_OPERATIONS_PAUSED", False):
        return False
    allowed = current_app.config.get("WALLET_OPERATION_ALLOWED_SESSION_ID", "")
    return not allowed or not secrets.compare_digest(allowed, session_id)


@bp.app_template_filter("sol")
def sol_filter(value):
    return sol_from_lamports(value)


@bp.app_template_filter("short")
def short_filter(value):
    if not value:
        return "—"
    value = str(value)
    return value if len(value) <= 18 else f"{value[:8]}…{value[-8:]}"


@bp.app_template_filter("status_label")
def status_label(value):
    return {
        "created": "Ready to send",
        "awaiting_payment": "Awaiting payment",
        "paid_exact": "Paid",
        "paid": "Paid",
        "underpaid": "Underpaid",
        "overpaid": "Overpaid",
        "refunded": "Refunded",
        "pending": "Pending",
        "exact": "Exact",
        "failed": "Failed",
        "payment_pending": "Payment in progress",
        "funding": "Payment in progress",
        "funding_pending": "Payment in progress",
        "funded": "Payment in progress",
        "complete": "Confirmed",
        "refund_processing": "Refund processing",
        "refund_submitted": "Refund submitted",
        "refund_finalized": "Refund finalized",
        "needs_review": "Needs Review",
        "authorized": "Reserved",
        "submitted": "Submitted",
        "finalized": "Finalized",
    }.get(value, str(value or "Unknown").replace("_", " ").title())


@bp.route("/")
def home():
    return render_template(
        "home.html",
        **template_context(
            page_title="RemaSol | SOL overpayment recovery",
            meta_description=(
                "RemaSol helps small merchants handle eligible SOL overpayments on "
                "Solana Devnet with guided customer recovery and less manual refund work."
            ),
        ),
    )


@bp.route("/merchant/login", methods=["GET", "POST"])
def merchant_login():
    if merchant_authenticated():
        return redirect(url_for("main.merchant_dashboard"))
    if request.method == "POST":
        if not csrf_ok():
            abort(400)
        if not merchant_is_configured():
            return render_template(
                "merchant_login.html",
                error="Merchant access is not configured on this server.",
                **template_context(),
            ), 503
        if not limiter().allow(f"merchant-login:{client_ip()}", 8):
            return render_template(
                "merchant_login.html",
                error="Too many login attempts. Wait a minute and try again.",
                **template_context(),
            ), 429
        if verify_merchant_credentials(
            request.form.get("username", "").strip(), request.form.get("password", "")
        ):
            session.clear()
            session["merchant_authenticated"] = True
            session["merchant_username"] = current_app.config["MERCHANT_USERNAME"]
            session["csrf_token"] = secrets.token_urlsafe(32)
            flash("Merchant console unlocked.", "success")
            return redirect(url_for("main.merchant_dashboard"))
        return render_template(
            "merchant_login.html",
            error="Merchant username or password is incorrect.",
            **template_context(),
        ), 401
    return render_template("merchant_login.html", **template_context())


@bp.post("/merchant/logout")
@merchant_required
def merchant_logout():
    if not csrf_ok():
        abort(400)
    session.clear()
    flash("Merchant console locked.", "success")
    return redirect(url_for("main.merchant_login"))


@bp.route("/merchant")
@merchant_required
def merchant_dashboard():
    products = engine().list_products()
    stats = engine().merchant_stats()
    payments = engine().list_merchant_payment_orders_recent()
    refunds = engine().list_refunds(limit=engine().MERCHANT_RECENT_LIMIT)
    orders = payments
    return render_template(
        "dashboard.html",
        products=products,
        orders=orders,
        payments=payments,
        refunds=refunds,
        stats=stats,
        recent_limit=engine().MERCHANT_RECENT_LIMIT,
        **template_context(),
    )


@bp.post("/merchant/payments/refresh")
@merchant_required
def refresh_merchant_payments():
    if not csrf_ok():
        abort(400)
    if not limiter().allow(f"merchant-payment-refresh:{client_ip()}", 20):
        flash("Payment refresh is temporarily rate-limited. Try again shortly.", "error")
        return redirect(url_for("main.merchant_dashboard"))
    try:
        payments, _refunds = engine().merchant_ledger()
        flash(f"Payment records refreshed from Devnet ({len(payments)} payment records).", "success")
    except Exception:
        # The dashboard remains usable if RPC is temporarily unavailable; the
        # next refresh can retry without changing any persisted payment state.
        flash("Devnet payment refresh is temporarily unavailable. Try again shortly.", "error")
    return redirect(url_for("main.merchant_dashboard"))


@bp.route("/merchant/treasury")
@merchant_required
def merchant_treasury():
    return render_template(
        "treasury.html",
        treasury=demo_service().treasury_dashboard(),
        **template_context(),
    )


@bp.post("/merchant/treasury/settings")
@merchant_required
def update_merchant_treasury():
    if not csrf_ok():
        abort(400)
    try:
        treasury = demo_service().update_settings(
            request.form.get("enabled") == "on",
            request.form.get("max_payment", ""),
            request.form.get("daily_budget", ""),
            request.form.get("max_concurrent", ""),
        )
    except DomainError as error:
        return render_template(
            "treasury.html",
            treasury=demo_service().treasury_dashboard(),
            error=str(error),
            **template_context(),
        ), 400
    flash("Demo treasury settings saved.", "success")
    return render_template("treasury.html", treasury=treasury, **template_context())


@bp.post("/merchant/products")
@merchant_required
def create_product():
    if not csrf_ok():
        abort(400)
    if not limiter().allow(f"product:{client_ip()}", 10):
        flash("Product creation is temporarily rate-limited. Try again shortly.", "error")
        return redirect(url_for("main.merchant_dashboard"))
    try:
        product = engine().create_product(
            request.form.get("name", ""),
            request.form.get("description", ""),
            request.form.get("price", ""),
        )
    except DomainError as error:
        return render_domain_error(
            "dashboard.html",
            error,
            products=engine().list_products(),
            orders=engine().list_merchant_payment_orders_recent(),
            payments=engine().list_merchant_payment_orders_recent(),
            refunds=engine().list_refunds(limit=engine().MERCHANT_RECENT_LIMIT),
            stats=engine().merchant_stats(),
            recent_limit=engine().MERCHANT_RECENT_LIMIT,
        )
    flash(f"{product['name']} is ready for its first checkout.", "success")
    return redirect(url_for("main.merchant_dashboard"))


@bp.route("/p/<slug>")
def product_checkout(slug):
    product = engine().get_product(slug)
    if not product:
        abort(404)
    return render_template("product.html", product=product, **template_context())


@bp.post("/p/<slug>/checkout")
def create_checkout(slug):
    if not csrf_ok():
        abort(400)
    if not limiter().allow(f"checkout:{client_ip()}", 10):
        product = engine().get_product(slug)
        return render_domain_error(
            "product.html",
            DomainError("Checkout creation is temporarily rate-limited. Try again shortly.", "rate_limited"),
            product=product,
        )
    try:
        order, claim_code = engine().create_order_draft(slug, client_ip())
    except DomainError as error:
        product = engine().get_product(slug)
        if not product:
            abort(404)
        return render_domain_error("product.html", error, product=product)
    session["checkout_access"] = order["id"]
    session["checkout_claim_code"] = claim_code
    return redirect(url_for("main.checkout_status", order_id=order["id"]))


@bp.route("/demo")
def demo_wallet():
    return render_template(
        "demo_guide.html",
        **template_context(
            page_title="RemaSol Demo | Recover a SOL overpayment",
            meta_description=(
                "Run a Solana Devnet overpayment test, see RemaSol identify the exact excess, "
                "and follow the eligible recovery and refund proof."
            ),
        ),
    )


@bp.route("/wallet")
def test_wallet():
    # Never restore a wallet from a long-lived browser cookie on the entry
    # page. Tests are selected only through their owner-checked URL below.
    session.pop("wallet_session_id", None)
    state = demo_service().wallet_page_state(wallet_browser_id())
    return render_template("wallet.html", **state, **template_context())


@bp.route("/wallet/history")
def wallet_history():
    """Keep old bookmarks from exposing the internal wallet ledger."""
    return redirect(url_for("main.test_wallet"))


@bp.route("/wallet/test/<session_id>")
def wallet_test(session_id):
    state = demo_service().wallet_page_state(
        wallet_browser_id(), session_id, include_terminal=True
    )
    if not state["wallet"]:
        abort(404)
    return render_template("wallet.html", **state, **template_context())


@bp.post("/wallet/create")
def create_test_wallet():
    if not csrf_ok():
        abort(400)
    if not limiter().allow(f"wallet-create:{client_ip()}", 20):
        state = demo_service().wallet_page_state(wallet_browser_id())
        return render_template(
            "wallet.html",
            **state,
            error="Test wallet creation is temporarily rate-limited. Try again shortly.",
            **template_context(),
        ), 429
    try:
        wallet = demo_service().create_test_wallet(wallet_browser_id(), client_ip())
    except DomainError as error:
        state = demo_service().wallet_page_state(wallet_browser_id())
        return render_template("wallet.html", **state, error=str(error), **template_context()), 400
    return redirect(url_for("main.wallet_test", session_id=wallet["id"]))


@bp.post("/wallet/send")
def send_test_wallet_payment():
    wants_json = wallet_json_request()
    if not csrf_ok():
        abort(400)
    requested_session_id = request.form.get("session_id", "").strip()
    if not requested_session_id:
        state = demo_service().wallet_page_state(wallet_browser_id())
        if wants_json:
            return wallet_json_response(
                state.get("wallet"),
                error="Open a New Test before sending test SOL.",
                status_code=400,
            )
        return render_template(
            "wallet.html",
            **state,
            error="Open a New Test before sending test SOL.",
            **template_context(),
        ), 400
    if wallet_operation_is_paused(requested_session_id):
        return wallet_maintenance_response(requested_session_id, wants_json)
    if not limiter().allow(f"wallet-send:{client_ip()}", 20):
        state = demo_service().wallet_page_state(
            wallet_browser_id(), requested_session_id, include_terminal=True
        )
        if wants_json:
            return wallet_json_response(
                state.get("wallet"),
                error="Test payment attempts are temporarily rate-limited. Try again shortly.",
                status_code=429,
            )
        return render_template(
            "wallet.html",
            **state,
            error="Test payment attempts are temporarily rate-limited. Try again shortly.",
            **template_context(),
        ), 429
    try:
        wallet = demo_service().send_test_wallet_payment(
            wallet_browser_id(),
            request.form.get("recipient_address", ""),
            request.form.get("amount", ""),
            client_ip(),
            requested_session_id,
        )
    except DomainError as error:
        state = demo_service().wallet_page_state(
            wallet_browser_id(), requested_session_id, include_terminal=True
        )
        if wants_json:
            return wallet_json_response(state.get("wallet"), error=str(error), status_code=400)
        return render_template("wallet.html", **state, error=str(error), **template_context()), 400
    if wants_json:
        return wallet_json_response(wallet)
    return redirect(url_for("main.wallet_test", session_id=wallet["id"]))


@bp.post("/wallet/recheck")
def recheck_test_wallet_payment():
    wants_json = wallet_json_request()
    if not csrf_ok():
        abort(400)
    requested_session_id = request.form.get("session_id", "").strip()
    if not requested_session_id:
        state = demo_service().wallet_page_state(wallet_browser_id())
        if wants_json:
            return wallet_json_response(
                state.get("wallet"),
                error="Open a saved Test Wallet session before checking payment status.",
                status_code=400,
            )
        return render_template(
            "wallet.html",
            **state,
            error="Open a saved Test Wallet session before checking payment status.",
            **template_context(),
        ), 400
    if wallet_operation_is_paused(requested_session_id):
        return wallet_maintenance_response(requested_session_id, wants_json)
    if not limiter().allow(f"wallet-recheck:{client_ip()}", 20):
        state = demo_service().wallet_page_state(
            wallet_browser_id(), requested_session_id, include_terminal=True
        )
        if wants_json:
            return wallet_json_response(
                state.get("wallet"),
                error="Payment status checks are temporarily rate-limited. Try again shortly.",
                status_code=429,
            )
        return render_template(
            "wallet.html",
            **state,
            error="Payment status checks are temporarily rate-limited. Try again shortly.",
            **template_context(),
        ), 429
    try:
        wallet = demo_service().reconcile_test_wallet_payment(
            wallet_browser_id(), requested_session_id
        )
    except DomainError as error:
        state = demo_service().wallet_page_state(
            wallet_browser_id(), requested_session_id, include_terminal=True
        )
        if wants_json:
            return wallet_json_response(state.get("wallet"), error=str(error), status_code=400)
        return render_template("wallet.html", **state, error=str(error), **template_context()), 400
    if wants_json:
        return wallet_json_response(wallet)
    return redirect(url_for("main.wallet_test", session_id=wallet["id"]))


@bp.post("/demo/create")
def create_demo_wallet():
    """Legacy endpoint retained for existing demo sessions and records."""
    if not csrf_ok():
        abort(400)
    if not limiter().allow(f"demo-create:{client_ip()}", 5):
        state = demo_service().page_state(demo_browser_id())
        return render_template(
            "demo.html",
            **state,
            error="Demo creation is temporarily rate-limited. Try again shortly.",
            **template_context(),
        ), 429
    try:
        demo, merchant_code = demo_service().create_demo(demo_browser_id(), client_ip())
    except DomainError as error:
        state = demo_service().page_state(demo_browser_id())
        return render_template("demo.html", **state, error=str(error), **template_context()), 400
    session["demo_session_id"] = demo["id"]
    session["demo_merchant_code"] = merchant_code
    flash("Demo wallet created. Save the Merchant Code before sending test SOL.", "success")
    return redirect(url_for("main.demo_wallet"))


@bp.post("/demo/send")
def send_demo_payment():
    if not csrf_ok():
        abort(400)
    if not limiter().allow(f"demo-send:{client_ip()}", 8):
        state = demo_service().page_state(demo_browser_id())
        return render_template(
            "demo.html",
            **state,
            merchant_code=session.get("demo_merchant_code", ""),
            error="Demo payment attempts are temporarily rate-limited. Try again shortly.",
            **template_context(),
        ), 429
    try:
        demo, verification = demo_service().run_demo(
            demo_browser_id(),
            session.get("demo_merchant_code", request.form.get("merchant_code", "")),
            request.form.get("amount", ""),
            client_ip(),
        )
    except AuthorizationError:
        state = demo_service().page_state(demo_browser_id())
        return render_template("demo.html", **state, error="The demo payment could not be authorized.", **template_context()), 403
    except DomainError as error:
        state = demo_service().page_state(demo_browser_id())
        return render_template(
            "demo.html",
            **state,
            merchant_code=session.get("demo_merchant_code", ""),
            error=str(error),
            **template_context(),
        ), 400
    state = demo_service().page_state(demo_browser_id())
    state["demo"] = demo
    return render_template(
        "demo.html",
        **state,
        merchant_code=session.get("demo_merchant_code", ""),
        verification=verification,
        **template_context(),
    )


@bp.route("/checkout/<order_id>")
def checkout_status(order_id):
    if session.get("checkout_access") != order_id:
        abort(403)
    order = engine().get_order(order_id)
    if not order:
        abort(404)
    return render_template(
        "checkout.html",
        order=order,
        claim_code=session.get("checkout_claim_code", ""),
        **template_context(),
    )


@bp.post("/checkout/<order_id>/verify")
def verify_checkout_payment(order_id):
    if not csrf_ok():
        abort(400)
    if not limiter().allow(f"verify:{client_ip()}", 15):
        order = engine().get_order(order_id)
        return render_template(
            "checkout.html",
            order=order,
            verification={"state": "needs_review", "reason": "Payment checks are temporarily rate-limited."},
            **template_context(),
        ), 429
    try:
        order, verification = engine().verify_payment(
            order_id,
            request.form.get("claim_code", ""),
            request.form.get("signature", ""),
        )
    except AuthorizationError:
        return render_template(
            "checkout.html",
            order=None,
            verification={"state": "invalid", "reason": "The checkout details could not be verified."},
            **template_context(),
        ), 403
    if verification.state == "exact":
        flash("Payment verified. The amount matches the product price.", "success")
    elif verification.state == "overpayment":
        flash("Payment verified. The excess is ready to recover.", "success")
    elif verification.state == "underpayment":
        flash("Payment verified, but it is below the product price.", "warning")
    return render_template(
        "checkout.html",
        order=order,
        verification=verification,
        claim_code=request.form.get("claim_code", ""),
        **template_context(),
    )


@bp.route("/recover")
def recover():
    return render_template("refund.html", **template_context())


@bp.post("/recover/check")
def check_recovery():
    if not csrf_ok():
        abort(400)
    if not limiter().allow(f"claim:{client_ip()}", current_app.config["MIN_CLAIM_ATTEMPTS_PER_MINUTE"]):
        return render_template(
            "refund.html",
            state="needs_review",
            message="Too many attempts from this network. Wait a minute before trying again.",
            **template_context(),
        ), 429
    merchant_code = request.form.get("merchant_code", request.form.get("claim_code", ""))
    sender_address = request.form.get("sender_address", request.form.get("reference", "")).strip()
    signature = request.form.get("signature", "").strip()
    if not merchant_code.strip() or not sender_address or not signature:
        return render_template(
            "refund.html",
            state="not_eligible",
            message="The payment details could not be verified. Enter the Merchant Code, sender wallet, and transaction ID.",
            **template_context(),
        ), 400
    try:
        order, state = engine().check_refund_eligibility_by_code(merchant_code, sender_address, signature)
    except AuthorizationError:
        return render_template(
            "refund.html",
            state="not_eligible",
            message="The payment details could not be verified. Check the Merchant Code, sender wallet, and transaction ID.",
            **template_context(),
        ), 403
    except DomainError as error:
        return render_template("refund.html", state="not_eligible", message=str(error), **template_context()), 400
    if state == "not_eligible":
        message = "The payment details could not be verified. Check the Merchant Code, sender wallet, and transaction ID."
    else:
        message = state_message(state, order)
    return render_template(
        "refund.html",
        order=order,
        state=state,
        message=message,
        sender_address=sender_address,
        merchant_code=merchant_code,
        signature=signature,
        **template_context(),
    )


@bp.post("/recover/request")
def request_recovery():
    if not csrf_ok():
        abort(400)
    if not limiter().allow(f"refund:{client_ip()}", 6):
        return render_template(
            "refund.html",
            state="needs_review",
            message="Refund requests are temporarily rate-limited. Your funds are unchanged.",
            **template_context(),
        ), 429
    merchant_code = request.form.get("merchant_code", request.form.get("claim_code", ""))
    sender_address = request.form.get("sender_address", request.form.get("reference", "")).strip()
    signature = request.form.get("signature", "").strip()
    if not merchant_code.strip() or not sender_address or not signature:
        return render_template(
            "refund.html",
            state="not_eligible",
            message="The payment details could not be verified. Enter the Merchant Code, sender wallet, and transaction ID.",
            **template_context(),
        ), 400
    try:
        order, state = engine().request_refund_by_code(merchant_code, sender_address, signature)
    except AuthorizationError:
        return render_template("refund.html", state="not_eligible", message="The payment details could not be verified.", **template_context()), 403
    except DomainError as error:
        order = engine().get_order_by_merchant_code(merchant_code)
        return render_template(
            "refund.html",
            order=order,
            state=error.code,
            message=str(error),
            sender_address=sender_address,
            merchant_code=merchant_code,
            signature=signature,
            **template_context(),
        ), 400
    if order.get("refund_id"):
        session["receipt_access"] = order["refund_id"]
        return redirect(url_for("main.receipt", refund_id=order["refund_id"]))
    return render_template(
        "refund.html",
        order=order,
        state=state,
        message=state_message(state, order),
        sender_address=sender_address,
        merchant_code=merchant_code,
        signature=signature,
        **template_context(),
    )


@bp.route("/receipt/<refund_id>")
def receipt(refund_id):
    if session.get("receipt_access") != refund_id:
        abort(403)
    try:
        order, state = engine().reconcile_refund(refund_id)
    except DomainError:
        abort(404)
    return render_template(
        "receipt.html",
        order=order,
        state=state,
        explorer_url=explorer_url(order.get("transaction_signature")),
        **template_context(),
    )


def state_message(state: str, order: dict | None) -> str:
    if state == "eligible":
        if order:
            return (
                f"RemaSol verified {order['payment_sol']} SOL sent to a {order['price_sol']} SOL checkout. "
                f"The {order['excess_sol']} SOL excess can be requested below."
            )
        return "The verified payment includes an eligible excess that can be requested below."
    if state == "underpaid":
        return "This payment is below the product price. RemaSol will not create an automatic payout."
    if state == "exact":
        return "This payment exactly matches the product price. There is no excess to return."
    if state == "submitted":
        return "The signed refund is on Devnet and waiting for finalization. Do not submit another request."
    if state == "finalized":
        return "The refund transaction is finalized on Solana Devnet."
    if state == "needs_review":
        return "This payment needs manual review. RemaSol has not created another payout request."
    return "No automatic refund is available for this payment."
