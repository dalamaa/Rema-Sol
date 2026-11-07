from __future__ import annotations

from .conftest import create_order


def csrf(client):
    client.get("/recover")
    with client.session_transaction() as session:
        return session["csrf_token"]


def login(client):
    token = csrf(client)
    response = client.post(
        "/merchant/login",
        data={"csrf_token": token, "username": "merchant", "password": "test-password"},
    )
    assert response.status_code == 302


def test_missing_csrf_token_is_rejected_with_safe_error_page(client):
    response = client.post(
        "/recover/check",
        data={"reference": "unknown", "claim_code": "wrong", "signature": "fake"},
    )

    assert response.status_code == 400
    assert b"Request could not be processed" in response.data


def test_login_rate_limit_stops_repeated_attempts(app, client):
    app.extensions["rate_limiter"].enabled = True
    token = csrf(client)
    statuses = [
        client.post(
            "/merchant/login",
            data={"csrf_token": token, "username": "merchant", "password": "wrong"},
        ).status_code
        for _ in range(9)
    ]

    assert statuses[:8] == [401] * 8
    assert statuses[8] == 429


def test_forwarded_for_spoof_does_not_bypass_claim_limit(app, client):
    app.extensions["rate_limiter"].enabled = True
    app.config["MIN_CLAIM_ATTEMPTS_PER_MINUTE"] = 1
    token = csrf(client)
    payload = {
        "csrf_token": token,
        "reference": "unknown",
        "claim_code": "wrong",
        "signature": "fake",
    }

    first = client.post(
        "/recover/check", data=payload, headers={"X-Forwarded-For": "198.51.100.10"}
    )
    second = client.post(
        "/recover/check", data=payload, headers={"X-Forwarded-For": "203.0.113.20"}
    )

    assert first.status_code == 403
    assert second.status_code == 429


def test_authenticated_post_still_requires_csrf(client):
    login(client)

    response = client.post(
        "/merchant/products",
        data={"name": "Missing token", "description": "", "price": "0.01"},
    )

    assert response.status_code == 400
    assert b"Request could not be processed" in response.data


def test_unpaid_checkout_cap_stops_checkout_spam(app, client, product):
    app.config["MAX_UNPAID_CHECKOUTS_PER_IP"] = 2
    app.extensions["rate_limiter"].enabled = True
    token = csrf(client)
    payload = {"csrf_token": token, "refund_destination": "refund-wallet"}

    first = client.post(f"/p/{product['slug']}/checkout", data=payload, follow_redirects=True)
    second = client.post(f"/p/{product['slug']}/checkout", data=payload, follow_redirects=True)
    third = client.post(f"/p/{product['slug']}/checkout", data=payload, follow_redirects=True)

    assert first.status_code == 200
    assert second.status_code == 200
    assert third.status_code == 400
    assert b"Finish or wait for an existing payment room" in third.data


def test_concurrent_refund_safety_persists_one_payout_candidate(engine, product, fake_solana):
    # The higher-volume engine concurrency test remains in test_engine.py. This
    # assertion makes the durable signature boundary explicit as well as the
    # one-broadcast assertion there.
    order, claim_code = create_order(engine, product, fake_solana)
    fake_solana.payments["one-candidate"] = {
        "destination": order["payment_address"],
        "amount": 500_000_000,
    }
    order, verification = engine.verify_payment(order["id"], claim_code, "one-candidate")
    assert verification.state == "overpayment"

    refunded, state = engine.request_refund(order["id"], claim_code, "one-candidate")

    assert state == "finalized"
    assert refunded["transaction_signature"] == "refund-signature-1"
    assert engine.get_order(order["id"])["transaction_signature"] == "refund-signature-1"
    assert fake_solana.broadcast_calls == 1


def test_finalized_receipt_route_renders_on_chain_proof(client, engine, product, fake_solana):
    order, claim_code = create_order(engine, product, fake_solana)
    fake_solana.payments["receipt-payment"] = {
        "destination": order["payment_address"],
        "amount": 500_000_000,
    }
    order, verification = engine.verify_payment(order["id"], claim_code, "receipt-payment")
    assert verification.state == "overpayment"
    refunded, state = engine.request_refund(order["id"], claim_code, "receipt-payment")
    assert state == "finalized"

    with client.session_transaction() as session:
        session["receipt_access"] = refunded["refund_id"]
    response = client.get(f"/receipt/{refunded['refund_id']}")

    assert response.status_code == 200
    assert b"Refund finalized" in response.data
    assert b"refund-signature-1" in response.data
