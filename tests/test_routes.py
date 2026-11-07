from __future__ import annotations

import re

from .conftest import add_payment, create_order


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
    assert response.location.endswith("/merchant")


def test_home_and_merchant_pages_render(client):
    assert client.get("/").status_code == 200
    assert client.get("/merchant").status_code == 302
    assert client.get("/merchant/login").status_code == 200
    login(client)
    assert client.get("/merchant").status_code == 200


def test_merchant_product_route_requires_login_and_creates_product(client):
    token = csrf(client)
    response = client.post(
        "/merchant/products",
        data={"csrf_token": token, "name": "Not allowed", "description": "", "price": "0.05"},
    )
    assert response.status_code == 302
    assert response.location.endswith("/merchant/login")
    login(client)
    with client.session_transaction() as session:
        token = session["csrf_token"]
    response = client.post(
        "/merchant/products",
        data={"csrf_token": token, "name": "Demo pack", "description": "A demo", "price": "0.05"},
    )
    assert response.status_code == 302
    assert response.location.endswith("/merchant")


def test_unconfigured_merchant_console_is_not_open(app):
    app.config["MERCHANT_PASSWORD_HASH"] = ""
    assert app.test_client().get("/merchant").status_code == 503


def test_product_and_checkout_route_render(app, client, engine, product, fake_solana):
    response = client.get(f"/p/{product['slug']}")
    assert response.status_code == 200
    order, claim_code = create_order(engine, product, fake_solana)
    with client.session_transaction() as session:
        session["checkout_access"] = order["id"]
    response = client.get(f"/checkout/{order['id']}")
    assert response.status_code == 200
    assert claim_code not in response.get_data(as_text=True)


def test_customer_flow_creates_a_payment_only_checkout(client, product):
    token = csrf(client)
    product_page = client.get(f"/p/{product['slug']}")
    product_html = product_page.get_data(as_text=True)
    assert product_page.status_code == 200
    assert "Buy with SOL" in product_html
    assert "Protect against overpayment" not in product_html

    checkout_page = client.post(
        f"/p/{product['slug']}/checkout", data={"csrf_token": token}, follow_redirects=True
    )
    assert checkout_page.status_code == 200
    checkout_html = checkout_page.get_data(as_text=True)
    assert "Unique payment wallet" in checkout_html
    assert "Private Merchant Code" in checkout_html
    assert "Copy Address" in checkout_html
    assert "Solana Devnet" in checkout_html
    assert "Refund protection" not in checkout_html
    assert "Protect against overpayment" not in checkout_html
    assert "Payment confirmation" not in checkout_html
    assert "transaction-signature" not in checkout_html
    assert checkout_page.request.path.startswith("/checkout/")
    with client.session_transaction() as session:
        order_id = session["checkout_access"]
    order = client.application.extensions["engine"].get_order(order_id)
    assert order["refund_destination"] == ""
    assert order["refund_destination_mode"] == "none"


def test_exchange_refund_guidance_is_visible_before_recovery_action(client, product):
    token = csrf(client)
    checkout = client.post(
        f"/p/{product['slug']}/checkout",
        data={"csrf_token": token},
        follow_redirects=True,
    ).get_data(as_text=True)
    exchange_note = "For easier refunds, pay from a Solana wallet you control."
    assert exchange_note in checkout
    assert checkout.index("payment-refund-note") < checkout.index("payment-actions")

    recovery = client.get("/recover").get_data(as_text=True)
    assert "does not guarantee that your exchange account receives the funds" in recovery
    assert "cannot reliably identify every exchange wallet" in recovery
    assert recovery.index("Exchange withdrawal note") < recovery.index("Check eligibility")


def test_each_customer_checkout_gets_unique_address_and_merchant_code(client, product):
    token = csrf(client)
    checkout_pages = [
        client.post(
            f"/p/{product['slug']}/checkout",
            data={"csrf_token": token},
            follow_redirects=True,
        ).get_data(as_text=True)
        for _index in range(2)
    ]
    addresses = [
        re.search(r'<code class="payment-address-code">([^<]+)</code>', page).group(1)
        for page in checkout_pages
    ]
    merchant_codes = [
        re.search(r'<div class="claim-code" data-copy="([^"]+)">', page).group(1)
        for page in checkout_pages
    ]
    assert len(set(addresses)) == 2
    assert len(set(merchant_codes)) == 2


def test_test_wallet_is_separate_and_only_sends_to_active_checkouts(client, app, engine, product, fake_solana):
    token = csrf(client)
    redirected = client.get("/demo")
    assert redirected.status_code == 302
    assert redirected.location.endswith("/wallet")
    assert "Merchant Code" not in client.get("/wallet").get_data(as_text=True)

    created = client.post("/wallet/create", data={"csrf_token": token})
    assert created.status_code == 302
    assert created.location.startswith("/wallet/test/")
    test_url = created.location
    wallet_html = client.get(test_url).get_data(as_text=True)
    for preset in ("0.001", "0.002", "0.004", "0.005"):
        assert preset in wallet_html
    with client.session_transaction() as session:
        token = session["csrf_token"]
        wallet_browser_id = session["wallet_browser_id"]
    session_id = test_url.rsplit("/", 1)[-1]
    entry_html = client.get("/wallet").get_data(as_text=True)
    assert "Test History" not in entry_html
    assert "Maximum Test Payment" in entry_html
    assert "recipient_address" not in entry_html

    rejected = client.post(
        "/wallet/send",
        data={"csrf_token": token, "session_id": session_id, "recipient_address": "external-wallet", "amount": "0.002"},
    )
    assert rejected.status_code == 400
    assert "active RemaSol checkout address" in rejected.get_data(as_text=True)
    assert fake_solana.broadcast_calls == 0

    order, _claim_code = engine.create_order_draft(product["slug"], "127.0.0.1")
    too_high = client.post(
        "/wallet/send",
        data={
            "csrf_token": token,
            "session_id": session_id,
            "recipient_address": order["payment_address"],
            "amount": "0.005001",
        },
    )
    assert too_high.status_code == 400
    assert "between 0.0005 and 0.005 SOL" in too_high.get_data(as_text=True)
    assert fake_solana.broadcast_calls == 0
    sent = client.post(
        "/wallet/send",
        data={
            "csrf_token": token,
            "session_id": session_id,
            "recipient_address": order["payment_address"],
            "amount": "0.005",
        },
    )
    assert sent.status_code == 302
    sent = client.get(sent.location)
    assert sent.status_code == 200
    sent_html = sent.get_data(as_text=True)
    sender = app.extensions["demo"]._wallet_session(browser_id=wallet_browser_id)["sender_address"]
    wallet_row = app.extensions["demo"]._wallet_session(browser_id=wallet_browser_id)
    assert wallet_row["funding_lamports"] == 5_000_000 + 890_880 + 100_000
    assert "Actual sender" in sent_html
    assert sender in sent_html
    assert order["payment_address"] in sent_html
    assert "0.005 SOL" in sent_html
    assert "transfer-signature-2" in sent_html
    assert "Open on Solana Explorer" in sent_html
    assert "Copy actual sender address" in sent_html
    assert "Copy recipient address" in sent_html
    assert "Copy transaction ID" in sent_html
    assert "Merchant Code" not in sent_html
    assert fake_solana.broadcast_calls == 2

    repeated = client.post(
        "/wallet/send",
        data={
            "csrf_token": token,
            "session_id": session_id,
            "recipient_address": order["payment_address"],
            "amount": "0.005",
        },
    )
    assert repeated.status_code == 302
    assert fake_solana.broadcast_calls == 2


def test_test_wallet_never_overwrites_a_locked_refund_destination(
    client, app, engine, product, fake_solana
):
    token = csrf(client)
    created = client.post("/wallet/create", data={"csrf_token": token})
    assert created.status_code == 302
    with client.session_transaction() as session:
        token = session["csrf_token"]
        browser_id = session["wallet_browser_id"]
    session_id = created.location.rsplit("/", 1)[-1]
    order, _claim_code = engine.create_demo_order(product["slug"], "locked-refund-wallet", "127.0.0.1")

    response = client.post(
        "/wallet/send",
        data={
            "csrf_token": token,
            "session_id": session_id,
            "recipient_address": order["payment_address"],
            "amount": "0.005",
        },
    )
    assert response.status_code == 400
    assert "already has a locked refund destination" in response.get_data(as_text=True)
    assert fake_solana.broadcast_calls == 0
    assert engine.get_order(order["id"])["refund_destination"] == "locked-refund-wallet"
    assert app.extensions["demo"]._wallet_session(browser_id=browser_id)["selected_lamports"] is None

def test_test_wallet_shows_per_payment_limit_without_balance_display(client, app, fake_solana):
    token = csrf(client)
    created = client.post("/wallet/create", data={"csrf_token": token})
    assert created.status_code == 302
    wallet_html = client.get("/wallet").get_data(as_text=True)
    assert "Maximum Test Payment" in wallet_html
    assert "This maximum resets for every payment" in wallet_html
    assert "0.005 <small>SOL" in wallet_html
    assert "Allocated today" not in wallet_html
    assert "Still unrecovered" not in wallet_html
    assert "earlier test attempts" not in wallet_html
    assert "duplicate broadcasts" not in wallet_html
    assert "Actual temporary wallet balance" not in wallet_html
    assert "Merchant Code" not in wallet_html

    test_page = client.get(created.location).get_data(as_text=True)
    assert "RemaSol provides free Devnet SOL for test payments." in test_page
    assert "Allocated today" not in test_page
    assert "Still unrecovered" not in test_page
    assert "Actual temporary wallet balance" not in test_page
    assert "on-chain balance of this temporary wallet" not in test_page
    settings = app.extensions["demo"]._settings()
    assert app.extensions["demo"]._parse_amount("0.0005", settings) == 500_000
    assert app.extensions["demo"]._parse_amount("0.005", settings) == 5_000_000


def test_uncertain_wallet_payment_recheck_never_broadcasts_again(client, engine, product, fake_solana):
    token = csrf(client)
    created = client.post("/wallet/create", data={"csrf_token": token})
    session_id = created.location.rsplit("/", 1)[-1]
    order, _claim_code = engine.create_order_draft(product["slug"], "127.0.0.1")
    with client.session_transaction() as session:
        token = session["csrf_token"]
    fake_solana.broadcast_fail_calls = {2}
    fake_solana.signature_statuses["transfer-signature-2"] = "unknown"
    uncertain = client.post(
        "/wallet/send",
        data={"csrf_token": token, "session_id": session_id, "recipient_address": order["payment_address"], "amount": "0.005"},
        follow_redirects=True,
    )
    assert uncertain.status_code == 200
    assert "Payment needs review" in uncertain.get_data(as_text=True)
    assert fake_solana.broadcast_calls == 2

    rechecked = client.post("/wallet/recheck", data={"csrf_token": token, "session_id": session_id}, follow_redirects=True)
    assert rechecked.status_code == 200
    assert "Payment needs review" in rechecked.get_data(as_text=True)
    assert fake_solana.broadcast_calls == 2

    fake_solana.signature_statuses["transfer-signature-2"] = "failed"
    failed = client.post("/wallet/recheck", data={"csrf_token": token, "session_id": session_id}, follow_redirects=True)
    assert failed.status_code == 200
    assert "Payment failed" in failed.get_data(as_text=True)
    assert fake_solana.broadcast_calls == 2


def test_new_test_creates_fresh_wallet_and_preserves_history(client, app, engine, product, fake_solana):
    token = csrf(client)
    created = client.post("/wallet/create", data={"csrf_token": token})
    first_session_id = created.location.rsplit("/", 1)[-1]
    with client.session_transaction() as session:
        token = session["csrf_token"]
        browser_id = session["wallet_browser_id"]
    first_url = f"/wallet/test/{first_session_id}"
    first_order, _claim_code = engine.create_order_draft(product["slug"], "127.0.0.1")
    sent = client.post(
        "/wallet/send",
        data={
            "csrf_token": token,
            "session_id": first_session_id,
            "recipient_address": first_order["payment_address"],
            "amount": "0.005",
        },
    )
    assert sent.status_code == 302
    first = app.extensions["demo"]._wallet_session(session_id=first_session_id)
    assert first["status"] == "complete"

    created = client.post("/wallet/create", data={"csrf_token": token})
    assert created.status_code == 302
    second_url = created.location
    second_session_id = second_url.rsplit("/", 1)[-1]
    second = app.extensions["demo"]._wallet_session(session_id=second_session_id)
    assert second_session_id != first_session_id
    assert second["sender_address"] != first["sender_address"]
    assert second["recipient_address"] is None

    page = client.get("/wallet").get_data(as_text=True)
    assert "New Test" in page
    assert "Test History" not in page
    assert first["sender_address"] not in page
    assert second["sender_address"] not in page
    assert "recipient_address" not in page
    second_page = client.get(second_url).get_data(as_text=True)
    assert second["sender_address"] in second_page
    assert 'id="wallet-recipient" name="recipient_address" value=""' in second_page
    history = client.get("/wallet/history")
    assert history.status_code == 302
    assert history.location.endswith("/wallet")
    assert client.get(first_url).get_data(as_text=True).count(first["sender_address"]) > 0
    assert app.extensions["demo"]._wallet_session(browser_id=browser_id)["id"] == second_session_id


def test_ten_sequential_tests_from_one_browser_get_fresh_wallets(client, app, engine, fake_solana):
    app.extensions["rate_limiter"].enabled = True
    app.extensions["demo"].update_settings(True, "0.005", "1", "3")
    token = csrf(client)
    demo_product = engine.get_product(app.config["DEMO_PRODUCT_SLUG"])
    amounts = ("0.001", "0.002", "0.004", "0.005")
    senders = []

    for index in range(10):
        created = client.post("/wallet/create", data={"csrf_token": token})
        assert created.status_code == 302
        session_id = created.location.rsplit("/", 1)[-1]
        order, _claim_code = engine.create_order_draft(demo_product["slug"], "127.0.0.1")
        sent = client.post(
            "/wallet/send",
            data={
                "csrf_token": token,
                "session_id": session_id,
                "recipient_address": order["payment_address"],
                "amount": amounts[index % len(amounts)],
            },
        )
        assert sent.status_code == 302
        wallet = app.extensions["demo"]._wallet_session(session_id=session_id)
        assert wallet["status"] == "complete"
        senders.append(wallet["sender_address"])

    assert len(set(senders)) == 10
    entry = client.get("/wallet").get_data(as_text=True)
    assert "Maximum Test Payment" in entry
    assert "Available Test Credit" not in entry
    assert "Test History" not in entry
    assert fake_solana.broadcast_calls == 20


def test_completed_and_failed_operations_release_concurrency_slot(client, app, engine, fake_solana):
    demo = app.extensions["demo"]
    demo.update_settings(True, "0.005", "1", "1")
    token = csrf(client)
    demo_product = engine.get_product(app.config["DEMO_PRODUCT_SLUG"])

    first = client.post("/wallet/create", data={"csrf_token": token})
    first_id = first.location.rsplit("/", 1)[-1]
    first_order, _claim_code = engine.create_order_draft(demo_product["slug"], "127.0.0.1")
    assert client.post(
        "/wallet/send",
        data={
            "csrf_token": token,
            "session_id": first_id,
            "recipient_address": first_order["payment_address"],
            "amount": "0.002",
        },
    ).status_code == 302
    assert demo._wallet_session(session_id=first_id)["status"] == "complete"

    second = client.post("/wallet/create", data={"csrf_token": token})
    second_id = second.location.rsplit("/", 1)[-1]
    second_order, _claim_code = engine.create_order_draft(demo_product["slug"], "127.0.0.1")
    assert client.post(
        "/wallet/send",
        data={
            "csrf_token": token,
            "session_id": second_id,
            "recipient_address": second_order["payment_address"],
            "amount": "0.002",
        },
    ).status_code == 302
    assert demo._wallet_session(session_id=second_id)["status"] == "complete"

    treasury = demo._settings()
    fake_solana.balances[treasury["public_address"]] = 0
    failed = client.post("/wallet/create", data={"csrf_token": token})
    failed_id = failed.location.rsplit("/", 1)[-1]
    failed_order, _claim_code = engine.create_order_draft(demo_product["slug"], "127.0.0.1")
    assert client.post(
        "/wallet/send",
        data={
            "csrf_token": token,
            "session_id": failed_id,
            "recipient_address": failed_order["payment_address"],
            "amount": "0.002",
        },
    ).status_code == 302
    assert demo._wallet_session(session_id=failed_id)["status"] == "failed"

    fake_solana.balances[treasury["public_address"]] = 2_000_000_000
    retry = client.post("/wallet/create", data={"csrf_token": token})
    retry_id = retry.location.rsplit("/", 1)[-1]
    retry_order, _claim_code = engine.create_order_draft(demo_product["slug"], "127.0.0.1")
    assert client.post(
        "/wallet/send",
        data={
            "csrf_token": token,
            "session_id": retry_id,
            "recipient_address": retry_order["payment_address"],
            "amount": "0.002",
        },
    ).status_code == 302
    assert demo._wallet_session(session_id=retry_id)["status"] == "complete"


def test_idle_and_old_failed_wallets_do_not_consume_operation_slots(client, app, engine, fake_solana):
    demo = app.extensions["demo"]
    demo.update_settings(True, "0.005", "1", "1")
    idle = demo.create_test_wallet("old-idle-browser", "127.0.0.1")
    failed = demo.create_test_wallet("old-failed-browser", "127.0.0.1")
    demo._mark_wallet(failed["id"], "failed", "historical failure")
    assert demo._wallet_session(session_id=idle["id"])["status"] == "created"

    token = csrf(client)
    created = client.post("/wallet/create", data={"csrf_token": token})
    session_id = created.location.rsplit("/", 1)[-1]
    demo_product = engine.get_product(app.config["DEMO_PRODUCT_SLUG"])
    order, _claim_code = engine.create_order_draft(demo_product["slug"], "127.0.0.1")
    response = client.post(
        "/wallet/send",
        data={
            "csrf_token": token,
            "session_id": session_id,
            "recipient_address": order["payment_address"],
            "amount": "0.002",
        },
    )
    assert response.status_code == 302
    assert demo._wallet_session(session_id=session_id)["status"] == "complete"


def test_unresolved_operation_keeps_slot_until_reconciled(client, app, engine, fake_solana):
    demo = app.extensions["demo"]
    demo.update_settings(True, "0.005", "1", "1")
    token = csrf(client)
    demo_product = engine.get_product(app.config["DEMO_PRODUCT_SLUG"])

    first = client.post("/wallet/create", data={"csrf_token": token})
    first_id = first.location.rsplit("/", 1)[-1]
    first_order, _claim_code = engine.create_order_draft(demo_product["slug"], "127.0.0.1")
    fake_solana.broadcast_fail_calls = {2}
    fake_solana.signature_statuses["transfer-signature-2"] = "unknown"
    uncertain = client.post(
        "/wallet/send",
        data={
            "csrf_token": token,
            "session_id": first_id,
            "recipient_address": first_order["payment_address"],
            "amount": "0.002",
        },
    )
    assert uncertain.status_code == 302
    assert demo._wallet_session(session_id=first_id)["status"] == "needs_review"
    assert fake_solana.broadcast_calls == 2

    second = client.post("/wallet/create", data={"csrf_token": token})
    second_id = second.location.rsplit("/", 1)[-1]
    second_order, _claim_code = engine.create_order_draft(demo_product["slug"], "127.0.0.1")
    blocked = client.post(
        "/wallet/send",
        data={
            "csrf_token": token,
            "session_id": second_id,
            "recipient_address": second_order["payment_address"],
            "amount": "0.002",
        },
    )
    assert blocked.status_code == 400
    assert "at capacity" in blocked.get_data(as_text=True)
    assert fake_solana.broadcast_calls == 2

    fake_solana.signature_statuses["transfer-signature-2"] = "failed"
    assert client.post(
        "/wallet/recheck",
        data={"csrf_token": token, "session_id": first_id},
    ).status_code == 302
    assert demo._wallet_session(session_id=first_id)["status"] == "failed"
    assert client.post(
        "/wallet/send",
        data={
            "csrf_token": token,
            "session_id": second_id,
            "recipient_address": second_order["payment_address"],
            "amount": "0.002",
        },
    ).status_code == 302
    assert demo._wallet_session(session_id=second_id)["status"] == "complete"


def test_wallet_test_urls_are_owner_scoped_and_entry_never_restores_old_session(
    client, app
):
    token = csrf(client)
    created = client.post("/wallet/create", data={"csrf_token": token})
    session_id = created.location.rsplit("/", 1)[-1]
    own_page = client.get(created.location).get_data(as_text=True)
    sender = app.extensions["demo"]._wallet_session(session_id=session_id)["sender_address"]
    assert sender in own_page
    assert sender not in client.get("/wallet").get_data(as_text=True)

    other = client.application.test_client()
    other_token = csrf(other)
    assert other.get(f"/wallet/test/{session_id}").status_code == 404
    denied = other.post(
        "/wallet/send",
        data={
            "csrf_token": other_token,
            "session_id": session_id,
            "recipient_address": "some-checkout",
            "amount": "0.005",
        },
    )
    assert denied.status_code == 400
    assert sender not in denied.get_data(as_text=True)


def test_terminal_failed_session_is_history_not_current_wallet(client, app, engine, product, fake_solana):
    token = csrf(client)
    created = client.post("/wallet/create", data={"csrf_token": token})
    session_id = created.location.rsplit("/", 1)[-1]
    order, _claim_code = engine.create_order_draft(product["slug"], "127.0.0.1")
    with client.session_transaction() as session:
        token = session["csrf_token"]
        browser_id = session["wallet_browser_id"]
    fake_solana.broadcast_fail_calls = {2}
    fake_solana.signature_statuses["transfer-signature-2"] = "unknown"
    uncertain = client.post(
        "/wallet/send",
        data={"csrf_token": token, "session_id": session_id, "recipient_address": order["payment_address"], "amount": "0.005"},
        follow_redirects=True,
    )
    assert "Payment needs review" in uncertain.get_data(as_text=True)
    fake_solana.signature_statuses["transfer-signature-2"] = "failed"
    assert client.post("/wallet/recheck", data={"csrf_token": token, "session_id": session_id}, follow_redirects=True).status_code == 200

    page = client.get("/wallet").get_data(as_text=True)
    assert "New Test" in page
    assert "Failed" not in page
    history = client.get("/wallet/history")
    assert history.status_code == 302
    assert app.extensions["demo"]._wallet_session(session_id=session_id)["status"] == "failed"
    assert "Payment failed" in client.get(f"/wallet/test/{session_id}").get_data(as_text=True)
    assert app.extensions["demo"]._wallet_session(browser_id=browser_id, session_id=session_id)["status"] == "failed"


def test_failed_allocation_releases_credit_only_after_sender_is_empty(client, app, engine, product, fake_solana):
    demo = app.extensions["demo"]
    demo.update_settings(True, "0.005", "0.006", "3")
    token = csrf(client)
    created = client.post("/wallet/create", data={"csrf_token": token})
    session_id = created.location.rsplit("/", 1)[-1]
    order, _claim_code = engine.create_order_draft(product["slug"], "127.0.0.1")
    with client.session_transaction() as session:
        token = session["csrf_token"]
        browser_id = session["wallet_browser_id"]
    fake_solana.broadcast_fail_calls = {2}
    fake_solana.signature_statuses["transfer-signature-2"] = "unknown"
    client.post(
        "/wallet/send",
        data={"csrf_token": token, "session_id": session_id, "recipient_address": order["payment_address"], "amount": "0.005"},
    )
    fake_solana.signature_statuses["transfer-signature-2"] = "failed"
    client.post("/wallet/recheck", data={"csrf_token": token, "session_id": session_id})
    failed = demo._wallet_session(browser_id=browser_id)
    fake_solana.balances[failed["sender_address"]] = failed["funding_lamports"]
    locked = demo.wallet_page_state(browser_id)
    assert locked["credit_accounting"]["unrecovered_funds_lamports"] == "0.00599088"
    assert "Maximum Test Payment" in client.get("/wallet").get_data(as_text=True)

    fake_solana.balances[failed["sender_address"]] = 0
    released = demo.wallet_page_state(browser_id)
    assert released["credit_accounting"]["unrecovered_funds_lamports"] == "0"

def test_merchant_dashboard_refreshes_payments_without_recovery_claim(client, engine, product, fake_solana):
    order, _claim_code = engine.create_order_draft(product["slug"], "127.0.0.1")
    add_payment(fake_solana, "dashboard-payment", order, 60_000_000)
    verified, verification = engine.verify_payment(order["id"], _claim_code, "dashboard-payment")
    assert verification.state == "overpayment"
    engine.request_refund(verified["id"], _claim_code, "dashboard-payment")
    login(client)
    with client.session_transaction() as session:
        token = session["csrf_token"]
    rpc_calls = []
    for method_name in ("get_signatures_for_address", "signature_status", "verify_payment"):
        original = getattr(fake_solana, method_name)

        def counted(*args, _original=original, _name=method_name, **kwargs):
            rpc_calls.append(_name)
            return _original(*args, **kwargs)

        setattr(fake_solana, method_name, counted)
    fast_dashboard = client.get("/merchant")
    assert fast_dashboard.status_code == 200
    assert rpc_calls == []
    refreshed = client.post("/merchant/payments/refresh", data={"csrf_token": token})
    assert refreshed.status_code == 302
    assert rpc_calls
    dashboard = client.get("/merchant").get_data(as_text=True)
    assert "Payments" in dashboard
    assert "Refunds" in dashboard
    assert "dashboard-payment"[:8] in dashboard
    assert order["payment_address"][:8] in dashboard
    assert "60 SOL" not in dashboard
    assert "0.06 SOL" in dashboard
    assert "manual review" in dashboard.lower()
    assert "Merchant Code" not in dashboard


def test_twenty_unpaid_checkouts_are_not_visible_in_merchant_activity(client, engine, product):
    token = csrf(client)
    unpaid_orders = []
    for _index in range(20):
        checkout = client.post(
            f"/p/{product['slug']}/checkout",
            data={"csrf_token": token},
            follow_redirects=True,
        )
        assert checkout.status_code == 200
        with client.session_transaction() as session:
            unpaid_orders.append(engine.get_order(session["checkout_access"]))

    login(client)
    dashboard = client.get("/merchant").get_data(as_text=True)
    payments_section = dashboard.split("<h2>Payments</h2>", 1)[1].split(
        "<h2>Refunds</h2>", 1
    )[0]
    assert "No payments received yet." in payments_section
    assert "Pending" not in payments_section
    for order in unpaid_orders:
        assert order["payment_address"] not in payments_section

    assert engine.merchant_ledger()[0] == []
    assert len(engine.list_orders()) == len(unpaid_orders)
    recent_activity = dashboard.split("<h2>Recent payment activity</h2>", 1)[1]
    assert "0 total" in recent_activity
    assert "No verified payments yet." in recent_activity
    assert "Awaiting payment" not in recent_activity
    assert "0 SOL" not in recent_activity
    assert engine.list_merchant_payment_orders() == []
    assert '<span class="metric-label">Payments</span><strong>0</strong>' in dashboard


def test_authenticated_dashboard_shows_verified_activity_and_keeps_unpaid_checkouts_internal(
    client, engine, product, fake_solana
):
    token = csrf(client)
    unpaid_orders = []
    for _index in range(20):
        checkout = client.post(
            f"/p/{product['slug']}/checkout",
            data={"csrf_token": token},
            follow_redirects=True,
        )
        with client.session_transaction() as session:
            unpaid_orders.append(engine.get_order(session["checkout_access"]))

    paid_order, claim_code = create_order(engine, product, fake_solana, "verified-sender")
    add_payment(fake_solana, "verified-dashboard-payment", paid_order, 50_000_000)
    fake_solana.payments["verified-dashboard-payment"]["source"] = "verified-sender"
    verified, result = engine.verify_payment(paid_order["id"], claim_code, "verified-dashboard-payment")
    assert result.state == "exact"

    login(client)
    dashboard = client.get("/merchant").get_data(as_text=True)
    payments_section = dashboard.split("<h2>Payments</h2>", 1)[1].split(
        "<h2>Refunds</h2>", 1
    )[0]
    recent_activity = dashboard.split("<h2>Recent payment activity</h2>", 1)[1]

    assert payments_section.count("verified…-payment") == 1
    assert payments_section.count(verified["payment_address"][:8]) == 1
    assert "0.05 SOL" in payments_section
    assert "Paid" in payments_section
    assert "verified-sender" in payments_section
    assert "1 total" in recent_activity
    assert f">{paid_order['payment_address']}</td>" in recent_activity
    assert "Awaiting payment" not in recent_activity
    for order in unpaid_orders:
        assert f">{order['payment_address']}</td>" not in recent_activity


def test_merchant_payment_statuses_use_verified_amounts_and_refund_state(
    client, engine, product, fake_solana
):
    amounts = {
        "underpaid-payment": 40_000_000,
        "paid-payment": 50_000_000,
        "overpaid-payment": 60_000_000,
    }
    orders = {}
    for signature, amount in amounts.items():
        order, code = create_order(engine, product, fake_solana, f"sender-{signature}")
        add_payment(fake_solana, signature, order, amount)
        verified, result = engine.verify_payment(order["id"], code, signature)
        assert result.state in {"underpayment", "exact", "overpayment"}
        orders[signature] = (verified, code)

    payments = {payment["payment_signature"]: payment for payment in engine.refresh_payment_records()}
    assert payments["underpaid-payment"]["payment_status"] == "underpaid"
    assert payments["paid-payment"]["payment_status"] == "paid"
    assert payments["overpaid-payment"]["payment_status"] == "overpaid"
    assert payments["paid-payment"]["sender_address"] == "exchange-hot-wallet"
    assert payments["paid-payment"]["received_lamports"] == 50_000_000
    assert payments["paid-payment"]["receiving_address"] == orders["paid-payment"][0]["payment_address"]
    assert payments["paid-payment"]["payment_timestamp"]

    engine.request_refund(
        orders["overpaid-payment"][0]["id"],
        orders["overpaid-payment"][1],
        "overpaid-payment",
    )
    refunded = {
        payment["payment_signature"]: payment
        for payment in engine.refresh_payment_records()
    }
    assert refunded["overpaid-payment"]["payment_status"] == "refunded"


def test_unconfirmed_wallet_payment_stays_durable_but_out_of_confirmed_payments(
    client, app, engine, product, fake_solana
):
    token = csrf(client)
    created = client.post("/wallet/create", data={"csrf_token": token})
    session_id = created.location.rsplit("/", 1)[-1]
    order, _claim_code = engine.create_order_draft(product["slug"], "127.0.0.1")
    with client.session_transaction() as session:
        token = session["csrf_token"]
        browser_id = session["wallet_browser_id"]
    fake_solana.signature_statuses["transfer-signature-2"] = "unknown"

    sent = client.post(
        "/wallet/send",
        data={
            "csrf_token": token,
            "session_id": session_id,
            "recipient_address": order["payment_address"],
            "amount": "0.005",
        },
    )
    assert sent.status_code == 302
    wallet = app.extensions["demo"]._wallet_session(
        browser_id=browser_id, session_id=session_id
    )
    assert wallet["payment_signature"] == "transfer-signature-2"
    assert engine.get_order(order["id"])["payment_signature"] is None

    login(client)
    dashboard = client.get("/merchant").get_data(as_text=True)
    payments_section = dashboard.split("<h2>Payments</h2>", 1)[1].split(
        "<h2>Refunds</h2>", 1
    )[0]
    assert "No payments received yet." in payments_section
    assert "transfer-signature-2" not in payments_section

def test_demo_product_price_changes_without_repricing_existing_order(app, engine):
    product = engine.get_product("the-digital-creator-s-guide")
    assert product["price_lamports"] == 2_000_000
    order, _claim_code = engine.create_order_draft(product["slug"], "127.0.0.1")

    connection = app.extensions["engine"]._connection()
    try:
        connection.execute(
            "UPDATE products SET price_lamports = ? WHERE slug = ?",
            (3_000_000, product["slug"]),
        )
    finally:
        connection.close()

    assert engine.get_order(order["id"])["price_lamports"] == 2_000_000


def test_rate_limits_repeated_claim_checks(app, client):
    app.extensions["rate_limiter"].enabled = True
    token = csrf(client)
    statuses = []
    for _index in range(14):
        response = client.post(
            "/recover/check",
            data={"csrf_token": token, "reference": "unknown", "claim_code": "bad", "signature": "bad"},
        )
        statuses.append(response.status_code)
    assert 429 in statuses


def test_public_demo_creates_realistic_wallet_flow_and_sender_bound_recovery(client, app, fake_solana):
    token = csrf(client)
    assert client.get("/demo").status_code == 302
    assert client.get("/wallet").status_code == 200

    created = client.post("/demo/create", data={"csrf_token": token})
    assert created.status_code == 302
    assert created.location.endswith("/demo")

    with client.session_transaction() as session:
        token = session["csrf_token"]
        merchant_code = session["demo_merchant_code"]
        browser_id = session["demo_browser_id"]

    demo_service = app.extensions["demo"]
    demo_row = demo_service._session(browser_id=browser_id)
    assert demo_row["buyer_address"] != demo_service._settings()["public_address"]

    sent = client.post(
        "/demo/send",
        data={"csrf_token": token, "amount": "0.005"},
    )
    assert sent.status_code == 200
    sent_html = sent.get_data(as_text=True)
    assert "Overpayment verified" in sent_html
    assert "transfer-signature-2" in sent_html
    assert fake_solana.broadcast_calls == 2

    demo_row = demo_service._session(browser_id=browser_id)
    assert demo_row["status"] == "overpaid"
    assert demo_row["funding_signature"] == "transfer-signature-1"
    assert demo_row["payment_signature"] == "transfer-signature-2"

    recovery = client.post(
        "/recover/check",
        data={
            "csrf_token": token,
            "merchant_code": merchant_code,
            "sender_address": demo_row["buyer_address"],
            "signature": demo_row["payment_signature"],
        },
    )
    assert recovery.status_code == 200
    assert "Eligible" in recovery.get_data(as_text=True)

    requested = client.post(
        "/recover/request",
        data={
            "csrf_token": token,
            "merchant_code": merchant_code,
            "sender_address": demo_row["buyer_address"],
            "signature": demo_row["payment_signature"],
        },
    )
    assert requested.status_code == 302
    assert requested.location.startswith("/receipt/")
    assert fake_solana.broadcast_calls == 3
    demo_order = app.extensions["engine"].get_order(demo_row["order_id"])
    assert demo_order["refund_destination"] == demo_row["buyer_address"]
    assert demo_order["refund_destination_mode"] == "internal_demo"

    mismatched = client.post(
        "/recover/check",
        data={
            "csrf_token": token,
            "merchant_code": merchant_code,
            "sender_address": "wrong-sender-wallet",
            "signature": demo_row["payment_signature"],
        },
    )
    assert mismatched.status_code == 200
    assert "The payment details could not be verified" in mismatched.get_data(as_text=True)


def test_test_wallet_to_refund_browser_journey(client, app, engine, fake_solana):
    """Exercise the supervised demo journey without broadcasting on Devnet."""
    token = csrf(client)
    product_slug = "the-digital-creator-s-guide"
    checkout = client.post(
        f"/p/{product_slug}/checkout",
        data={"csrf_token": token},
        follow_redirects=True,
    )
    assert checkout.status_code == 200
    checkout_html = checkout.get_data(as_text=True)
    assert "0.002 SOL" in checkout_html
    assert "Unique payment wallet" in checkout_html
    assert "Private Merchant Code" in checkout_html

    with client.session_transaction() as session:
        order_id = session["checkout_access"]
        merchant_code = session["checkout_claim_code"]
        token = session["csrf_token"]
    order = engine.get_order(order_id)
    assert order["payment_address"]
    assert order["refund_destination"] == ""

    created = client.post("/wallet/create", data={"csrf_token": token})
    assert created.status_code == 302
    with client.session_transaction() as session:
        token = session["csrf_token"]
        browser_id = session["wallet_browser_id"]
    session_id = created.location.rsplit("/", 1)[-1]
    wallet = app.extensions["demo"]._wallet_session(browser_id=browser_id)
    sender = wallet["sender_address"]
    assert sender != order["payment_address"]
    test_url = f"/wallet/test/{session_id}"
    assert 'name="recipient_address" value=""' in client.get(test_url).get_data(as_text=True)

    observed_before_broadcast = []
    original_broadcast = fake_solana.broadcast_signed_transaction

    def observe_binding(signed_transaction):
        observed_before_broadcast.append(engine.get_order(order_id))
        return original_broadcast(signed_transaction)

    fake_solana.broadcast_signed_transaction = observe_binding
    sent = client.post(
        "/wallet/send",
        data={
            "csrf_token": token,
            "session_id": session_id,
            "recipient_address": order["payment_address"],
            "amount": "0.005",
        },
    )
    assert sent.status_code == 302
    sent = client.get(sent.location)
    assert "transfer-signature-2" in sent.get_data(as_text=True)
    assert observed_before_broadcast
    assert observed_before_broadcast[0]["refund_destination"] == sender
    assert observed_before_broadcast[0]["refund_destination_mode"] == "internal_demo"
    assert observed_before_broadcast[0]["sender_ownership_verified"] == 1

    wallet = app.extensions["demo"]._wallet_session(browser_id=browser_id)
    assert wallet["status"] == "complete"
    payment_signature = wallet["payment_signature"]
    order = engine.get_order(order_id)
    assert order["payment_signature"] == payment_signature
    assert order["payment_source_address"] == sender
    assert order["payment_lamports"] == 5_000_000

    # Merchant refresh is exercised before any recovery claim is checked.
    login(client)
    with client.session_transaction() as session:
        token = session["csrf_token"]
    assert client.post(
        "/merchant/payments/refresh", data={"csrf_token": token}
    ).status_code == 302
    merchant_page = client.get("/merchant").get_data(as_text=True)
    assert payment_signature in merchant_page
    assert order["payment_address"] in merchant_page
    assert "0.005 SOL" in merchant_page

    recovery = client.post(
        "/recover/check",
        data={
            "csrf_token": token,
            "merchant_code": merchant_code,
            "sender_address": sender,
            "signature": payment_signature,
        },
    )
    assert recovery.status_code == 200
    recovery_html = recovery.get_data(as_text=True)
    assert "Eligible" in recovery_html
    assert "0.003" in recovery_html
    assert sender in recovery_html

    requested = client.post(
        "/recover/request",
        data={
            "csrf_token": token,
            "merchant_code": merchant_code,
            "sender_address": sender,
            "signature": payment_signature,
        },
    )
    assert requested.status_code == 302
    assert requested.location.startswith("/receipt/")
    assert fake_solana.broadcast_calls == 3

    order = engine.get_order(order_id)
    assert order["refund_amount_lamports"] == 3_000_000
    assert order["refund_destination"] == sender
    assert order["refund_status"] == "finalized"
    refunds = engine.list_refunds()
    assert len(refunds) == 1
    assert refunds[0]["amount_lamports"] == 3_000_000
    assert refunds[0]["destination"] == sender
    assert refunds[0]["transaction_signature"] == "refund-signature-3"
    assert refunds[0]["status"] == "finalized"

    repeated = client.post(
        "/recover/request",
        data={
            "csrf_token": token,
            "merchant_code": merchant_code,
            "sender_address": sender,
            "signature": payment_signature,
        },
    )
    assert repeated.status_code == 302
    assert repeated.location == requested.location
    assert fake_solana.broadcast_calls == 3

    merchant_page = client.get("/merchant").get_data(as_text=True)
    assert "0.003 SOL" in merchant_page
    assert sender in merchant_page
    assert "refund-signature-3" in merchant_page
    assert "Finalized" in merchant_page


def test_demo_treasury_is_authenticated_and_configurable(client):
    assert client.get("/merchant/treasury").status_code == 302
    login(client)
    treasury_page = client.get("/merchant/treasury")
    assert treasury_page.status_code == 200
    treasury_html = treasury_page.get_data(as_text=True)
    assert "Allocated today" in treasury_html
    assert "Still unrecovered" in treasury_html
    with client.session_transaction() as session:
        token = session["csrf_token"]
    updated = client.post(
        "/merchant/treasury/settings",
        data={
            "csrf_token": token,
            "enabled": "",
            "max_payment": "0.005",
            "daily_budget": "0.1",
            "max_concurrent": "2",
        },
    )
    assert updated.status_code == 200
    assert "Demo paused" in updated.get_data(as_text=True)
