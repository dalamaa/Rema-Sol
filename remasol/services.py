from __future__ import annotations

import base64
import secrets
import sqlite3
import uuid
from datetime import timedelta
from typing import Any

from .db import begin_immediate, commit, connect, rollback
from .security import (
    decrypt_private_key,
    encrypt_private_key,
    generate_claim_code,
    hash_claim_code,
    iso_now,
    utc_now,
    verify_claim_code,
)
from .solana_service import (
    PaymentVerification,
    RefundPreparation,
    SolanaRpcError,
)


class DomainError(RuntimeError):
    def __init__(self, message: str, code: str = "invalid"):
        super().__init__(message)
        self.code = code


class AuthorizationError(DomainError):
    def __init__(self):
        super().__init__("The checkout details could not be verified.", "unauthorized")


def lamports_from_sol(value: str) -> int:
    cleaned = str(value).strip()
    if not cleaned:
        raise DomainError("Enter a price in SOL.", "validation")
    try:
        whole, _, fraction = cleaned.partition(".")
        if whole.startswith("+") or whole.startswith("-"):
            raise ValueError
        if not whole.isdigit() or len(fraction) > 9 or (fraction and not fraction.isdigit()):
            raise ValueError
        fraction = (fraction + "0" * 9)[:9]
        amount = int(whole) * 1_000_000_000 + int(fraction or 0)
    except (TypeError, ValueError):
        raise DomainError("Use a positive SOL amount with up to 9 decimals.", "validation")
    if amount <= 0:
        raise DomainError("The price must be greater than zero.", "validation")
    return amount


def sol_from_lamports(value: int | None) -> str:
    if value is None:
        return "0"
    whole, remainder = divmod(int(value), 1_000_000_000)
    if not remainder:
        return str(whole)
    return f"{whole}.{remainder:09d}".rstrip("0")


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row else None


class PaymentEngine:
    def __init__(self, db_path: str, secret_key: str, wallet_encryption_key: str, solana, config):
        self.db_path = db_path
        self.secret_key = secret_key
        self.wallet_encryption_key = wallet_encryption_key
        self.solana = solana
        self.config = config

    def _connection(self):
        return connect(self.db_path)

    def create_product(self, name: str, description: str, price: str) -> dict[str, Any]:
        name = (name or "").strip()
        description = (description or "").strip()
        if not name or len(name) > 120:
            raise DomainError("Give the product a name under 120 characters.", "validation")
        if len(description) > 800:
            raise DomainError("Keep the product description under 800 characters.", "validation")
        price_lamports = lamports_from_sol(price)
        slug = self._unique_slug(name)
        product = {
            "id": str(uuid.uuid4()),
            "slug": slug,
            "name": name,
            "description": description,
            "price_lamports": price_lamports,
            "price_sol": sol_from_lamports(price_lamports),
            "created_at": iso_now(),
        }
        connection = self._connection()
        try:
            connection.execute(
                "INSERT INTO products (id, slug, name, description, price_lamports, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    product["id"],
                    product["slug"],
                    product["name"],
                    product["description"],
                    product["price_lamports"],
                    product["created_at"],
                ),
            )
        finally:
            connection.close()
        return product

    def _unique_slug(self, name: str) -> str:
        base = "-".join("".join(char.lower() if char.isalnum() else "-" for char in name).split("-"))
        base = base[:48].strip("-") or "product"
        connection = self._connection()
        try:
            slug = base
            suffix = 2
            while connection.execute("SELECT 1 FROM products WHERE slug = ?", (slug,)).fetchone():
                slug = f"{base}-{suffix}"
                suffix += 1
            return slug
        finally:
            connection.close()

    def list_products(self) -> list[dict[str, Any]]:
        connection = self._connection()
        try:
            rows = connection.execute("SELECT * FROM products ORDER BY created_at DESC").fetchall()
            return [{**dict(row), "price_sol": sol_from_lamports(row["price_lamports"])} for row in rows]
        finally:
            connection.close()

    def get_product(self, slug: str) -> dict[str, Any] | None:
        connection = self._connection()
        try:
            row = connection.execute("SELECT * FROM products WHERE slug = ?", (slug,)).fetchone()
            if not row:
                return None
            return {**dict(row), "price_sol": sol_from_lamports(row["price_lamports"])}
        finally:
            connection.close()

    def create_order(self, product_slug: str, refund_destination: str, client_ip: str) -> tuple[dict[str, Any], str]:
        product = self.get_product(product_slug)
        if not product:
            raise DomainError("That product is not available.", "not_found")
        refund_destination = (refund_destination or "").strip()
        if not self.solana.validate_address(refund_destination):
            raise DomainError("Enter a valid Solana refund destination.", "validation")

        connection = self._connection()
        try:
            cutoff = (utc_now() - timedelta(minutes=self.config["CHECKOUT_EXPIRY_MINUTES"])).isoformat()
            outstanding = connection.execute(
                "SELECT COUNT(*) AS count FROM orders WHERE client_ip = ? AND status = 'awaiting_payment' AND created_at > ?",
                (client_ip, cutoff),
            ).fetchone()["count"]
            if outstanding >= self.config["MAX_UNPAID_CHECKOUTS_PER_IP"]:
                raise DomainError("Finish or wait for an existing payment room before creating another.", "rate_limited")
        finally:
            connection.close()

        if not self.wallet_encryption_key:
            raise DomainError(
                "Wallet encryption is not configured. Add REMA_SOL_WALLET_KEY to .env first.",
                "configuration",
            )
        payment_address, private_key = self.solana.generate_wallet()
        claim_code = generate_claim_code()
        now = iso_now()
        order = {
            "id": str(uuid.uuid4()),
            "product_id": product["id"],
            "payment_address": payment_address,
            "refund_destination": refund_destination,
            "status": "awaiting_payment",
            "expires_at": (utc_now() + timedelta(minutes=self.config["CHECKOUT_EXPIRY_MINUTES"])).isoformat(),
            "created_at": now,
            "updated_at": now,
            "product_name": product["name"],
            "product_description": product["description"],
            "price_lamports": product["price_lamports"],
            "price_sol": product["price_sol"],
        }
        connection = self._connection()
        try:
            connection.execute(
                """INSERT INTO orders
                (id, product_id, payment_address, encrypted_private_key, claim_code_hash,
                 refund_destination, client_ip, status, expires_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    order["id"],
                    product["id"],
                    payment_address,
                    encrypt_private_key(private_key, self.wallet_encryption_key),
                    hash_claim_code(claim_code),
                    refund_destination,
                    client_ip,
                    order["status"],
                    order["expires_at"],
                    now,
                    now,
                ),
            )
        finally:
            connection.close()
        return order, claim_code

    def get_order(self, order_id: str) -> dict[str, Any] | None:
        connection = self._connection()
        try:
            row = connection.execute(
                """SELECT o.*, p.name AS product_name, p.slug AS product_slug, p.description AS product_description,
                p.price_lamports, r.id AS refund_id, r.amount_lamports AS refund_amount_lamports,
                r.destination AS refund_destination_locked, r.status AS refund_status,
                r.signed_transaction, r.transaction_signature, r.last_error, r.finalized_at
                FROM orders o JOIN products p ON p.id = o.product_id
                LEFT JOIN refunds r ON r.order_id = o.id
                WHERE o.id = ?""",
                (order_id,),
            ).fetchone()
            if not row:
                return None
            result = dict(row)
            result["price_sol"] = sol_from_lamports(result["price_lamports"])
            result["payment_sol"] = sol_from_lamports(result["payment_lamports"])
            result["excess_lamports"] = max(
                (result["payment_lamports"] or 0) - result["price_lamports"], 0
            )
            result["excess_sol"] = sol_from_lamports(result["excess_lamports"])
            return result
        finally:
            connection.close()

    def get_order_by_reference(self, reference: str) -> dict[str, Any] | None:
        reference = (reference or "").strip()
        connection = self._connection()
        try:
            row = connection.execute(
                "SELECT id FROM orders WHERE id = ? OR payment_address = ?",
                (reference, reference),
            ).fetchone()
            return self.get_order(row["id"]) if row else None
        finally:
            connection.close()

    def list_orders(self) -> list[dict[str, Any]]:
        connection = self._connection()
        try:
            rows = connection.execute(
                """SELECT o.*, p.name AS product_name, p.price_lamports,
                r.status AS refund_status, r.transaction_signature
                FROM orders o JOIN products p ON p.id = o.product_id
                LEFT JOIN refunds r ON r.order_id = o.id
                ORDER BY o.created_at DESC"""
            ).fetchall()
            output = []
            for row in rows:
                value = dict(row)
                value["price_sol"] = sol_from_lamports(value["price_lamports"])
                value["payment_sol"] = sol_from_lamports(value["payment_lamports"])
                output.append(value)
            return output
        finally:
            connection.close()

    def _authorize(self, order: dict[str, Any] | None, claim_code: str) -> dict[str, Any]:
        if not order or not claim_code or not verify_claim_code(claim_code.strip(), order["claim_code_hash"]):
            raise AuthorizationError()
        return order

    def verify_payment(self, order_id: str, claim_code: str, signature: str) -> tuple[dict[str, Any], PaymentVerification]:
        order = self._authorize(self.get_order(order_id), claim_code)
        if order["status"] == "awaiting_payment" and order["expires_at"] < iso_now():
            connection = self._connection()
            try:
                connection.execute(
                    "UPDATE orders SET status = 'expired', updated_at = ? WHERE id = ? AND status = 'awaiting_payment'",
                    (iso_now(), order_id),
                )
            finally:
                connection.close()
            return self.get_order(order_id), PaymentVerification(
                "invalid", signature=signature, reason="This unpaid checkout has expired"
            )
        signature = (signature or "").strip()
        if not self.solana.validate_signature(signature):
            return order, PaymentVerification("invalid", signature=signature, reason="Invalid transaction signature")

        connection = self._connection()
        try:
            assigned = connection.execute(
                "SELECT id FROM orders WHERE payment_signature = ?", (signature,)
            ).fetchone()
            if assigned and assigned["id"] != order_id:
                return order, PaymentVerification("needs_review", signature=signature, reason="This transaction is already assigned to another checkout")
        finally:
            connection.close()

        verification = self.solana.verify_payment(signature, order["payment_address"], order["price_lamports"])
        if verification.state not in {"exact", "underpayment", "overpayment"}:
            return order, verification

        connection = self._connection()
        try:
            begin_immediate(connection)
            current = connection.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
            assigned = connection.execute(
                "SELECT id FROM orders WHERE payment_signature = ?", (signature,)
            ).fetchone()
            if assigned and assigned["id"] != order_id:
                rollback(connection)
                return order, PaymentVerification("needs_review", signature=signature, reason="This transaction is already assigned to another checkout")
            if current["payment_signature"] and current["payment_signature"] != signature:
                rollback(connection)
                return self.get_order(order_id), PaymentVerification("needs_review", signature=signature, reason="A different payment is already verified for this checkout")
            status = {
                "exact": "paid_exact",
                "underpayment": "underpaid",
                "overpayment": "overpaid",
            }[verification.state]
            now = iso_now()
            connection.execute(
                """UPDATE orders SET status = ?, payment_signature = ?, payment_source_address = ?,
                payment_lamports = ?, verified_at = ?, updated_at = ? WHERE id = ?""",
                (
                    status,
                    signature,
                    verification.source_address,
                    verification.amount_lamports,
                    now,
                    now,
                    order_id,
                ),
            )
            commit(connection)
        except sqlite3.IntegrityError:
            rollback(connection)
            return self.get_order(order_id), PaymentVerification("needs_review", signature=signature, reason="This payment could not be assigned safely")
        except Exception:
            rollback(connection)
            raise
        finally:
            connection.close()
        return self.get_order(order_id), verification

    def check_refund_eligibility(
        self, reference: str, claim_code: str, signature: str
    ) -> tuple[dict[str, Any], str]:
        order = self._authorize(self.get_order_by_reference(reference), claim_code)
        signature = (signature or "").strip()
        if order["payment_signature"] != signature:
            order, verification = self.verify_payment(order["id"], claim_code, signature)
            if verification.state == "needs_review":
                return order, "needs_review"
            if verification.state == "invalid":
                return order, "not_eligible"
        order = self.get_order(order["id"])
        if order.get("refund_status"):
            return order, order["refund_status"]
        if order["status"] == "overpaid":
            return order, "eligible"
        if order["status"] == "underpaid":
            return order, "underpaid"
        if order["status"] == "paid_exact":
            return order, "exact"
        if order["status"] in {"needs_review", "expired"}:
            return order, "needs_review"
        return order, "not_eligible"

    def request_refund(
        self, reference: str, claim_code: str, signature: str
    ) -> tuple[dict[str, Any], str]:
        order, eligibility = self.check_refund_eligibility(reference, claim_code, signature)
        if eligibility == "needs_review" and not order.get("refund_id"):
            raise DomainError(self._eligibility_message(eligibility), "needs_review")
        if eligibility not in {"eligible", "authorized", "broadcast", "submitted", "finalized", "needs_review"}:
            raise DomainError(self._eligibility_message(eligibility), "not_eligible")
        if order.get("refund_id"):
            return self.reconcile_refund(order["refund_id"])

        amount = order["excess_lamports"]
        try:
            balance = self.solana.get_balance(order["payment_address"])
        except SolanaRpcError as error:
            return self._create_review_refund(order, amount, f"Balance check failed: {error}")
        required = amount + self.config["REFUND_FEE_BUFFER_LAMPORTS"]
        if balance < required:
            return self._create_review_refund(
                order,
                amount,
                f"Checkout wallet has {sol_from_lamports(balance)} SOL available; {sol_from_lamports(required)} SOL is required for the refund and fee buffer.",
            )

        try:
            private_key = decrypt_private_key(order["encrypted_private_key"], self.wallet_encryption_key)
            preparation: RefundPreparation = self.solana.prepare_refund(
                private_key, order["refund_destination"], amount
            )
        except Exception as error:
            return self._create_review_refund(order, amount, f"Refund could not be prepared safely: {error}")

        refund_id = str(uuid.uuid4())
        now = iso_now()
        connection = self._connection()
        try:
            begin_immediate(connection)
            existing = connection.execute(
                "SELECT id FROM refunds WHERE order_id = ?", (order["id"],)
            ).fetchone()
            if existing:
                rollback(connection)
                return self.reconcile_refund(existing["id"])
            connection.execute(
                """INSERT INTO refunds
                (id, order_id, amount_lamports, destination, status, signed_transaction,
                 transaction_signature, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'authorized', ?, ?, ?, ?)""",
                (
                    refund_id,
                    order["id"],
                    amount,
                    order["refund_destination"],
                    base64.b64encode(preparation.signed_transaction).decode(),
                    preparation.transaction_signature,
                    now,
                    now,
                ),
            )
            connection.execute(
                "UPDATE orders SET status = 'refund_processing', updated_at = ? WHERE id = ?",
                (now, order["id"]),
            )
            commit(connection)
        except sqlite3.IntegrityError:
            rollback(connection)
            existing = connection.execute("SELECT id FROM refunds WHERE order_id = ?", (order["id"],)).fetchone()
            if existing:
                return self.reconcile_refund(existing["id"])
            raise DomainError("The refund could not be reserved safely.", "needs_review")
        except Exception:
            rollback(connection)
            raise
        finally:
            connection.close()

        try:
            broadcast_signature = self.solana.broadcast_signed_transaction(preparation.signed_transaction)
        except Exception as error:
            # The signed bytes and signature are durable. Never build a new tx from here.
            self._update_refund(refund_id, "needs_review", f"Broadcast outcome is uncertain: {error}")
            try:
                saved_status = self.solana.signature_status(preparation.transaction_signature)
            except Exception:
                saved_status = "unknown"
            if saved_status in {"confirmed", "finalized"}:
                self._update_refund(refund_id, "submitted")
            return self.reconcile_refund(refund_id)

        if broadcast_signature and broadcast_signature != preparation.transaction_signature:
            self._update_refund(refund_id, "needs_review", "RPC returned a different signature than the signed transaction")
            return self.reconcile_refund(refund_id)
        self._update_refund(refund_id, "submitted")
        return self.reconcile_refund(refund_id)

    def _create_review_refund(self, order: dict[str, Any], amount: int, reason: str):
        connection = self._connection()
        now = iso_now()
        try:
            begin_immediate(connection)
            existing = connection.execute("SELECT id FROM refunds WHERE order_id = ?", (order["id"],)).fetchone()
            if existing:
                rollback(connection)
                return self.reconcile_refund(existing["id"])
            refund_id = str(uuid.uuid4())
            connection.execute(
                """INSERT INTO refunds (id, order_id, amount_lamports, destination, status, last_error, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'needs_review', ?, ?, ?)""",
                (refund_id, order["id"], amount, order["refund_destination"], reason, now, now),
            )
            connection.execute(
                "UPDATE orders SET status = 'needs_review', updated_at = ? WHERE id = ?",
                (now, order["id"]),
            )
            commit(connection)
        finally:
            connection.close()
        return self.reconcile_refund(refund_id)

    def _update_refund(self, refund_id: str, status: str, last_error: str | None = None):
        connection = self._connection()
        try:
            now = iso_now()
            connection.execute(
                "UPDATE refunds SET status = ?, last_error = ?, updated_at = ? WHERE id = ?",
                (status, last_error, now, refund_id),
            )
            order_status = "refund_submitted" if status == "submitted" else "needs_review" if status == "needs_review" else None
            if order_status:
                connection.execute(
                    "UPDATE orders SET status = ?, updated_at = ? WHERE id = (SELECT order_id FROM refunds WHERE id = ?)",
                    (order_status, now, refund_id),
                )
        finally:
            connection.close()

    def reconcile_refund(self, refund_id: str) -> tuple[dict[str, Any], str]:
        connection = self._connection()
        try:
            row = connection.execute("SELECT * FROM refunds WHERE id = ?", (refund_id,)).fetchone()
        finally:
            connection.close()
        if not row:
            raise DomainError("Refund record not found.", "not_found")
        refund = dict(row)
        if refund["status"] == "submitted" and refund["transaction_signature"]:
            try:
                status = self.solana.signature_status(refund["transaction_signature"])
            except SolanaRpcError:
                status = "unknown"
            if status == "finalized":
                connection = self._connection()
                try:
                    now = iso_now()
                    connection.execute(
                        "UPDATE refunds SET status = 'finalized', finalized_at = ?, updated_at = ? WHERE id = ?",
                        (now, now, refund_id),
                    )
                    connection.execute(
                        "UPDATE orders SET status = 'refund_finalized', updated_at = ? WHERE id = ?",
                        (now, refund["order_id"]),
                    )
                finally:
                    connection.close()
            elif status == "failed":
                self._update_refund(refund_id, "needs_review", "The signed refund transaction failed on Solana; manual review is required.")
        order = self.get_order(refund["order_id"])
        refreshed = self._get_refund(refund_id)
        return order, refreshed["status"]

    def _get_refund(self, refund_id: str) -> dict[str, Any]:
        connection = self._connection()
        try:
            row = connection.execute("SELECT * FROM refunds WHERE id = ?", (refund_id,)).fetchone()
            return dict(row) if row else {}
        finally:
            connection.close()

    @staticmethod
    def _eligibility_message(state: str) -> str:
        return {
            "underpaid": "This payment is below the product price, so there is no excess to return.",
            "exact": "This payment exactly matches the product price. No refund is due.",
            "not_eligible": "This payment is not eligible for an automatic refund.",
            "needs_review": "This payment needs manual review. No new payout request was created.",
        }.get(state, "This payment is not eligible for an automatic refund.")
