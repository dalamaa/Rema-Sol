from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from remasol.services import AuthorizationError, DomainError
from remasol.solana_service import PaymentVerification

from .conftest import add_payment, create_order


LAMPORTS_PER_SOL = 1_000_000_000


def test_successful_payment_verification_exact_under_and_over(engine, product, fake_solana):
    exact, exact_code = create_order(engine, product, fake_solana)
    add_payment(fake_solana, "sig-exact", exact, 50_000_000)
    exact_after, result = engine.verify_payment(exact["id"], exact_code, "sig-exact")
    assert result.state == "exact"
    assert exact_after["status"] == "paid_exact"
    assert exact_after["payment_lamports"] == 50_000_000

    under, under_code = create_order(engine, product, fake_solana, "refund-wallet-2")
    add_payment(fake_solana, "sig-under", under, 40_000_000)
    under_after, result = engine.verify_payment(under["id"], under_code, "sig-under")
    assert result.state == "underpayment"
    assert under_after["status"] == "underpaid"

    over, over_code = create_order(engine, product, fake_solana, "refund-wallet-3")
    add_payment(fake_solana, "sig-over", over, 500_000_000)
    over_after, result = engine.verify_payment(over["id"], over_code, "sig-over")
    assert result.state == "overpayment"
    assert over_after["status"] == "overpaid"
    assert over_after["excess_lamports"] == 450_000_000


def test_invalid_signature_wrong_destination_and_ambiguous_transfer(engine, product, fake_solana):
    order, code = create_order(engine, product, fake_solana)
    _, invalid = engine.verify_payment(order["id"], code, "bad")
    assert invalid.state == "invalid"

    fake_solana.payments["wrong-room"] = {
        "destination": "another-payment-wallet",
        "amount": 500_000_000,
    }
    _, wrong_room = engine.verify_payment(order["id"], code, "wrong-room")
    assert wrong_room.state == "needs_review"

    fake_solana.payments["ambiguous"] = {
        "destination": order["payment_address"],
        "amount": 500_000_000,
        "state": "needs_review",
    }
    _, ambiguous = engine.verify_payment(order["id"], code, "ambiguous")
    assert ambiguous.state == "needs_review"


def test_wrong_claim_code_and_copied_signature_cannot_unlock_another_order(engine, product, fake_solana):
    first, first_code = create_order(engine, product, fake_solana)
    second, second_code = create_order(engine, product, fake_solana, "refund-wallet-2")
    add_payment(fake_solana, "copied-signature", first, 500_000_000)

    with pytest.raises(AuthorizationError):
        engine.verify_payment(first["id"], "not-the-code", "copied-signature")

    _, copied = engine.verify_payment(second["id"], second_code, "copied-signature")
    assert copied.state == "needs_review"
    assert engine.get_order(second["id"])["payment_signature"] is None


def verify_overpayment(engine, product, fake_solana, signature="sig-over"):
    order, code = create_order(engine, product, fake_solana)
    add_payment(fake_solana, signature, order, 500_000_000)
    order, result = engine.verify_payment(order["id"], code, signature)
    assert result.state == "overpayment"
    return order, code


def test_idempotent_refund_and_explorer_identity(engine, product, fake_solana):
    order, code = verify_overpayment(engine, product, fake_solana)
    refunded, state = engine.request_refund(order["id"], code, "sig-over")
    assert state == "finalized"
    assert refunded["refund_status"] == "finalized"
    assert refunded["refund_amount_lamports"] == 450_000_000
    assert fake_solana.broadcast_calls == 1

    again, again_state = engine.request_refund(order["id"], code, "sig-over")
    assert again_state == "finalized"
    assert again["refund_id"] == refunded["refund_id"]
    assert fake_solana.broadcast_calls == 1


def test_concurrent_requests_authorize_only_one_broadcast(engine, product, fake_solana):
    order, code = verify_overpayment(engine, product, fake_solana, "sig-concurrent")

    def request_refund():
        return engine.request_refund(order["id"], code, "sig-concurrent")

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(lambda _index: request_refund(), range(32)))

    assert fake_solana.broadcast_calls == 1
    assert len({value[0]["refund_id"] for value in results}) == 1
    assert len({value[0]["transaction_signature"] for value in results}) == 1
    assert engine.get_order(order["id"])["refund_status"] == "finalized"


def test_timeout_after_broadcast_persists_signed_transaction_and_never_rebuilds(engine, product, fake_solana):
    order, code = verify_overpayment(engine, product, fake_solana, "sig-timeout")
    fake_solana.broadcast_timeout = True
    fake_solana.refund_status = "unknown"
    first, first_state = engine.request_refund(order["id"], code, "sig-timeout")
    assert first_state == "needs_review"
    assert first["refund_status"] == "needs_review"
    assert first["signed_transaction"]
    saved_signature = first["transaction_signature"]
    assert fake_solana.broadcast_calls == 1

    fake_solana.broadcast_timeout = False
    second, second_state = engine.request_refund(order["id"], code, "sig-timeout")
    assert second_state == "needs_review"
    assert second["transaction_signature"] == saved_signature
    assert fake_solana.broadcast_calls == 1


def test_restart_keeps_uncertain_refund_manual_review(engine, product, fake_solana, app):
    order, code = verify_overpayment(engine, product, fake_solana, "sig-restart")
    fake_solana.broadcast_timeout = True
    fake_solana.refund_status = "unknown"
    engine.request_refund(order["id"], code, "sig-restart")

    # A new engine instance represents a server restart. It sees the durable record.
    restarted = app.extensions["engine"].__class__(
        app.config["DATABASE"],
        app.config["SECRET_KEY"],
        app.config["WALLET_ENCRYPTION_KEY"],
        fake_solana,
        app.config,
    )
    fake_solana.broadcast_timeout = False
    recovered, state = restarted.request_refund(order["id"], code, "sig-restart")
    assert state == "needs_review"
    assert recovered["refund_status"] == "needs_review"
    assert fake_solana.broadcast_calls == 1


def test_insufficient_balance_is_review_only(engine, product, fake_solana):
    order, code = verify_overpayment(engine, product, fake_solana, "sig-balance")
    fake_solana.balances[order["payment_address"]] = 100_000_000
    result, state = engine.request_refund(order["id"], code, "sig-balance")
    assert state == "needs_review"
    assert result["refund_status"] == "needs_review"
    assert result["signed_transaction"] is None
    assert fake_solana.broadcast_calls == 0


def test_invalid_refund_destination_is_rejected(engine, product, fake_solana):
    with pytest.raises(DomainError):
        engine.create_order(product["slug"], "", "127.0.0.1")


def test_ordinary_checkout_derives_sender_and_routes_unproven_refund_to_review(engine, product, fake_solana):
    order, code = engine.create_order_draft(product["slug"], "127.0.0.1")
    add_payment(fake_solana, "ordinary-overpayment", order, 500_000_000)

    verified, verification = engine.verify_payment(order["id"], code, "ordinary-overpayment")
    assert verification.state == "overpayment"
    assert verified["refund_destination"] == "exchange-hot-wallet"
    assert verified["refund_destination_mode"] == "derived_sender"
    assert verified["sender_ownership_verified"] == 0

    checked, eligibility = engine.check_refund_eligibility_by_code(
        code, "exchange-hot-wallet", "ordinary-overpayment"
    )
    assert eligibility == "needs_review"
    reviewed, state = engine.request_refund_by_code(
        code, "exchange-hot-wallet", "ordinary-overpayment"
    )
    assert state == "needs_review"
    assert reviewed["refund_status"] == "needs_review"
    assert reviewed["refund_destination"] == "exchange-hot-wallet"
    assert fake_solana.broadcast_calls == 0


def test_claimant_cannot_replace_derived_refund_destination(engine, product, fake_solana):
    order, code = engine.create_order_draft(product["slug"], "127.0.0.1")
    with pytest.raises(DomainError, match="derived from the verified payment sender"):
        engine.set_refund_destination(order["id"], code, "arbitrary-refund-wallet")
