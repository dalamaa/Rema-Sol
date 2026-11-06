from __future__ import annotations

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
