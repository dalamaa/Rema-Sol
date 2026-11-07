from __future__ import annotations

from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from werkzeug.security import generate_password_hash

from remasol import create_app
from remasol.solana_service import PaymentVerification, RefundPreparation


class FakeSolanaService:
    def __init__(self):
        self.wallet_count = 0
        self.payments = {}
        self.balances = {}
        self.prepared = {}
        self.private_to_public = {}
        self.prepare_calls = 0
        self.broadcast_calls = 0
        self.broadcast_timeout = False
        self.broadcast_fail_calls = set()
        self.refund_status = "finalized"
        self.signature_statuses = {}

    def validate_address(self, address):
        return bool(address and 3 <= len(address) <= 120 and " " not in address)

    def validate_signature(self, signature):
        return bool(signature and signature != "bad")

    def generate_wallet(self):
        self.wallet_count += 1
        address = f"payment-wallet-{self.wallet_count}"
        private_key = f"private-key-{self.wallet_count}".encode()
        self.private_to_public[private_key] = address
        return address, private_key

    def verify_payment(self, signature, destination, expected_lamports):
        payment = self.payments.get(signature)
        if not payment:
            return PaymentVerification("invalid", signature=signature, reason="Transaction not found")
        if payment["destination"] != destination:
            return PaymentVerification("needs_review", signature=signature, reason="Wrong destination")
        if payment.get("state") == "needs_review":
            return PaymentVerification("needs_review", signature=signature, reason="Ambiguous transfer")
        amount = payment["amount"]
        state = "exact" if amount == expected_lamports else "overpayment" if amount > expected_lamports else "underpayment"
        return PaymentVerification(state, amount, payment.get("source", "source-wallet"), signature)

    def get_balance(self, address):
        return self.balances.get(address, 2_000_000_000)

    def get_minimum_balance_for_rent_exemption(self, data_length=0):
        return 890_880

    def prepare_refund(self, private_key, destination, amount_lamports):
        self.prepare_calls += 1
        signature = f"refund-signature-{self.prepare_calls}"
        signed = f"signed-{signature}".encode()
        self.prepared[signed] = signature
        return RefundPreparation(signed, signature)

    def prepare_transfer(self, private_key, destination, amount_lamports):
        self.prepare_calls += 1
        signature = f"transfer-signature-{self.prepare_calls}"
        signed = f"signed-{signature}".encode()
        self.prepared[signed] = signature
        self.payments[signature] = {
            "destination": destination,
            "amount": amount_lamports,
            "source": self.private_to_public.get(private_key, "source-wallet"),
        }
        return RefundPreparation(signed, signature)

    def broadcast_signed_transaction(self, signed_transaction):
        self.broadcast_calls += 1
        if self.broadcast_timeout:
            raise TimeoutError("simulated connection loss after broadcast")
        if self.broadcast_calls in self.broadcast_fail_calls:
            raise RuntimeError("simulated preflight rejection")
        return self.prepared[signed_transaction]

    def signature_status(self, signature):
        return self.signature_statuses.get(signature, self.refund_status)

    def get_signatures_for_address(self, address, limit=20):
        rows = []
        for signature, payment in reversed(list(self.payments.items())):
            if payment.get("destination") != address:
                continue
            status = self.signature_status(signature)
            rows.append(
                {
                    "signature": signature,
                    "err": {"simulated": True} if status == "failed" else None,
                    "confirmationStatus": None if status == "failed" else status,
                    "blockTime": 1_700_000_000,
                }
            )
        return rows[:limit]


@pytest.fixture
def fake_solana():
    return FakeSolanaService()


@pytest.fixture
def app(tmp_path: Path, fake_solana):
    return create_app(
        {
            "TESTING": True,
            "DATABASE": str(tmp_path / "test.sqlite3"),
            "SECRET_KEY": "test-session-secret",
            "WALLET_ENCRYPTION_KEY": Fernet.generate_key().decode(),
            "MERCHANT_USERNAME": "merchant",
            "MERCHANT_PASSWORD_HASH": generate_password_hash("test-password"),
            "SOLANA_SERVICE": fake_solana,
            "RATE_LIMIT_ENABLED": False,
            "CHECKOUT_EXPIRY_MINUTES": 60,
            "MAX_UNPAID_CHECKOUTS_PER_IP": 100,
            "REFUND_FEE_BUFFER_LAMPORTS": 10_000,
        }
    )


@pytest.fixture
def engine(app):
    return app.extensions["engine"]


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def product(engine):
    return engine.create_product("Field Notes pack", "A small digital field guide.", "0.05")


def create_order(engine, product, fake_solana, destination="refund-wallet"):
    order, claim_code = engine.create_order(product["slug"], destination, "127.0.0.1")
    order = engine.get_order(order["id"])
    return order, claim_code


def add_payment(fake_solana, signature, order, amount):
    fake_solana.payments[signature] = {
        "destination": order["payment_address"],
        "amount": amount,
        "source": "exchange-hot-wallet",
    }
