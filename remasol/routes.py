from __future__ import annotations

import secrets

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
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


def client_ip() -> str:
    # ProxyFix is enabled only when REMA_SOL_TRUST_PROXY=1. Never trust a raw
    # X-Forwarded-For header from an internet client for rate-limit identity.
    return request.remote_addr or "unknown"


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
        "awaiting_payment": "Awaiting payment",
        "paid_exact": "Paid exactly",
        "underpaid": "Underpaid",
        "overpaid": "Overpayment",
        "refund_processing": "Refund processing",
        "refund_submitted": "Refund submitted",
        "refund_finalized": "Refund finalized",
        "needs_review": "Needs review",
        "authorized": "Reserved",
        "submitted": "Submitted",
        "finalized": "Finalized",
    }.get(value, str(value or "Unknown").replace("_", " ").title())


@bp.route("/")
def home():
    return render_template("home.html", **template_context())


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
    orders = engine().list_orders()
    stats = {
        "products": len(products),
        "orders": len(orders),
        "overpayments": sum(order["status"] == "overpaid" for order in orders),
        "refunded": sum(order.get("refund_status") == "finalized" for order in orders),
    }
    return render_template(
        "dashboard.html",
        products=products,
        orders=orders,
        stats=stats,
        **template_context(),
    )


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
            orders=engine().list_orders(),
            stats={"products": 0, "orders": 0, "overpayments": 0, "refunded": 0},
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
        order, claim_code = engine().create_order(
            slug,
            request.form.get("refund_destination", ""),
            client_ip(),
        )
    except DomainError as error:
        product = engine().get_product(slug)
        if not product:
            abort(404)
        return render_domain_error("product.html", error, product=product)
    session["checkout_access"] = order["id"]
    return render_template(
        "checkout.html",
        order=engine().get_order(order["id"]),
        claim_code=claim_code,
        just_created=True,
        **template_context(),
    )


@bp.route("/checkout/<order_id>")
def checkout_status(order_id):
    if session.get("checkout_access") != order_id:
        abort(403)
    order = engine().get_order(order_id)
    if not order:
        abort(404)
    return render_template("checkout.html", order=order, **template_context())


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
    reference = request.form.get("reference", "").strip()
    claim_code = request.form.get("claim_code", "")
    signature = request.form.get("signature", "").strip()
    try:
        order, state = engine().check_refund_eligibility(reference, claim_code, signature)
    except AuthorizationError:
        return render_template(
            "refund.html",
            state="not_eligible",
            message="The checkout details could not be verified. Check the address, claim code, and transaction signature.",
            **template_context(),
        ), 403
    except DomainError as error:
        return render_template("refund.html", state="not_eligible", message=str(error), **template_context()), 400
    return render_template(
        "refund.html",
        order=order,
        state=state,
        message=state_message(state, order),
        reference=reference,
        claim_code=claim_code,
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
    reference = request.form.get("reference", "").strip()
    claim_code = request.form.get("claim_code", "")
    signature = request.form.get("signature", "").strip()
    try:
        order, state = engine().request_refund(reference, claim_code, signature)
    except AuthorizationError:
        return render_template("refund.html", state="not_eligible", message="The checkout details could not be verified.", **template_context()), 403
    except DomainError as error:
        order = engine().get_order_by_reference(reference)
        return render_template(
            "refund.html",
            order=order,
            state=error.code,
            message=str(error),
            reference=reference,
            claim_code=claim_code,
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
        reference=reference,
        claim_code=claim_code,
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
        return "The payment is above the product price. The refundable excess is ready to return to the locked destination."
    if state == "underpaid":
        return "This payment is below the product price. RemaSol will not create an automatic payout."
    if state == "exact":
        return "This payment exactly matches the product price. There is no excess to return."
    if state == "submitted":
        return "The signed refund is on Devnet and waiting for finalization."
    if state == "finalized":
        return "The refund transaction is finalized on Solana Devnet."
    if state == "needs_review":
        return "This payment needs manual review. RemaSol has not created another payout request."
    return "No automatic refund is available for this payment."
