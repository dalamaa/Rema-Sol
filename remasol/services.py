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
    MERCHANT_RECENT_LIMIT = 50
    VERIFIED_PAYMENT_STATUSES = (
        "paid_exact",
        "underpaid",
        "overpaid",
        "refund_processing",
        "refund_submitted",
        "refund_finalized",
        "needs_review",
    )

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

    def get_active_checkout_by_payment_address(self, payment_address: str) -> dict[str, Any] | None:
        """Return only an unpaid, unexpired checkout owned by RemaSol."""
        payment_address = (payment_address or "").strip()
        if not payment_address:
            return None
        connection = self._connection()
        try:
            row = connection.execute(
                "SELECT id FROM orders WHERE payment_address = ?",
                (payment_address,),
            ).fetchone()
        finally:
            connection.close()
        if not row:
            return None
        order = self.get_order(row["id"])
        if not order:
            return None
        if order["status"] != "awaiting_payment":
            return None
        if order.get("payment_signature") or order["expires_at"] <= iso_now():
            return None
        return order

    def create_order(self, product_slug: str, refund_destination: str, client_ip: str) -> tuple[dict[str, Any], str]:
        """Create a trusted pre-bound order for existing integrations.

        Public customer checkout uses ``create_order_draft``. This entry point
        remains for trusted callers such as the demo and legacy integrations
        that already supply a controlled refund destination.
        """
        return self._create_order(
            product_slug,
            refund_destination,
            client_ip,
            allow_empty=False,
            destination_mode="legacy_locked",
            sender_ownership_verified=True,
        )

    def create_demo_order(
        self, product_slug: str, refund_destination: str, client_ip: str
    ) -> tuple[dict[str, Any], str]:
        """Create a demo order whose refund destination is app-controlled."""
        return self._create_order(
            product_slug,
            refund_destination,
            client_ip,
            allow_empty=False,
            destination_mode="internal_demo",
            sender_ownership_verified=True,
        )

    def create_order_draft(self, product_slug: str, client_ip: str) -> tuple[dict[str, Any], str]:
        """Create an ordinary payment room without a refund destination."""
        return self._create_order(
            product_slug,
            "",
            client_ip,
            allow_empty=True,
            destination_mode="none",
            sender_ownership_verified=False,
        )

    def _create_order(
        self,
        product_slug: str,
        refund_destination: str,
        client_ip: str,
        *,
        allow_empty: bool,
        destination_mode: str,
        sender_ownership_verified: bool,
    ) -> tuple[dict[str, Any], str]:
        product = self.get_product(product_slug)
        if not product:
            raise DomainError("That product is not available.", "not_found")
        refund_destination = (refund_destination or "").strip()
        if (not allow_empty or refund_destination) and not self.solana.validate_address(refund_destination):
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
            "refund_destination_mode": destination_mode,
            "sender_ownership_verified": int(sender_ownership_verified),
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
                 refund_destination, refund_destination_mode, sender_ownership_verified,
                 client_ip, status, expires_at, price_lamports, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    order["id"],
                    product["id"],
                    payment_address,
                    encrypt_private_key(private_key, self.wallet_encryption_key),
                    hash_claim_code(claim_code),
                    refund_destination,
                    destination_mode,
                    int(sender_ownership_verified),
                    client_ip,
                    order["status"],
                    order["expires_at"],
                    product["price_lamports"],
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
                p.price_lamports AS product_price_lamports, r.id AS refund_id, r.amount_lamports AS refund_amount_lamports,
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
            if result.get("price_lamports") is None:
                result["price_lamports"] = result["product_price_lamports"]
            result["price_sol"] = sol_from_lamports(result["price_lamports"])
            result["payment_sol"] = sol_from_lamports(result["payment_lamports"])
            result["excess_lamports"] = max(
                (result["payment_lamports"] or 0) - result["price_lamports"], 0
            )
            result["excess_sol"] = sol_from_lamports(result["excess_lamports"])
            return result
        finally:
            connection.close()

    def set_refund_destination(
        self, order_id: str, claim_code: str, refund_destination: str
    ) -> dict[str, Any]:
        """Reject customer-supplied refund destinations.

        Refund destinations are either bound by trusted application code (the
        funded demo) or derived from the verified payment sender. A claimant
        must never be able to replace either value from checkout.
        """
        self._authorize(self.get_order(order_id), claim_code)
        raise DomainError(
            "Refund destinations are derived from the verified payment sender.",
            "unsupported",
        )

    def bind_demo_refund_destination(
        self,
        order_id: str,
        sender_address: str,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        """Atomically lock an app-generated Test Wallet sender to a checkout.

        This is intentionally an internal-only operation.  The public recovery
        forms cannot call it, and an existing non-demo destination is never
        replaced.  ``connection`` lets the Test Wallet reserve its sender and
        this binding in one SQLite transaction before any treasury funding is
        broadcast.
        """
        sender_address = (sender_address or "").strip()
        if not self.solana.validate_address(sender_address):
            raise DomainError("The generated Test Wallet sender is invalid.", "configuration")

        owns_connection = connection is None
        connection = connection or self._connection()
        try:
            if owns_connection:
                begin_immediate(connection)
            row = connection.execute(
                "SELECT * FROM orders WHERE id = ?", (order_id,)
            ).fetchone()
            if not row:
                raise DomainError("That checkout no longer exists.", "not_found")

            existing_destination = (row["refund_destination"] or "").strip()
            if existing_destination:
                if (
                    existing_destination == sender_address
                    and row["refund_destination_mode"] == "internal_demo"
                    and row["sender_ownership_verified"]
                ):
                    if owns_connection:
                        commit(connection)
                    return dict(row)
                raise DomainError(
                    "This checkout already has a locked refund destination.",
                    "locked",
                )
            if row["status"] != "awaiting_payment" or row["payment_signature"]:
                raise DomainError(
                    "This checkout is no longer available for a new Test Wallet payment.",
                    "locked",
                )

            now = iso_now()
            updated = connection.execute(
                """UPDATE orders SET refund_destination = ?,
                refund_destination_mode = 'internal_demo', sender_ownership_verified = 1,
                updated_at = ? WHERE id = ? AND status = 'awaiting_payment'
                AND payment_signature IS NULL AND refund_destination = ''""",
                (sender_address, now, order_id),
            )
            if updated.rowcount != 1:
                raise DomainError(
                    "This checkout could not be locked to the Test Wallet safely.",
                    "locked",
                )
            if owns_connection:
                commit(connection)
            return dict(connection.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone())
        except Exception:
            if owns_connection:
                rollback(connection)
            raise
        finally:
            if owns_connection:
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

    def get_order_by_merchant_code(self, merchant_code: str) -> dict[str, Any] | None:
        """Find an order without storing the bearer Merchant Code in plaintext."""
        merchant_code = (merchant_code or "").strip()
        if not merchant_code:
            return None
        connection = self._connection()
        try:
            rows = connection.execute(
                "SELECT id, claim_code_hash FROM orders ORDER BY created_at DESC"
            ).fetchall()
        finally:
            connection.close()
        for row in rows:
            if verify_claim_code(merchant_code, row["claim_code_hash"]):
                return self.get_order(row["id"])
        return None

    def list_orders(self) -> list[dict[str, Any]]:
        connection = self._connection()
        try:
            rows = connection.execute(
                """SELECT o.*, p.name AS product_name, p.price_lamports AS product_price_lamports,
                r.status AS refund_status, r.transaction_signature
                FROM orders o JOIN products p ON p.id = o.product_id
                LEFT JOIN refunds r ON r.order_id = o.id
                ORDER BY o.created_at DESC"""
            ).fetchall()
            output = []
            for row in rows:
                value = dict(row)
                if value.get("price_lamports") is None:
                    value["price_lamports"] = value["product_price_lamports"]
                value["price_sol"] = sol_from_lamports(value["price_lamports"])
                value["payment_sol"] = sol_from_lamports(value["payment_lamports"])
                value["payment_status"] = self._payment_status(
                    value["status"], value.get("refund_status")
                )
                output.append(value)
            return output
        finally:
            connection.close()

    def list_merchant_payment_orders(self, limit: int | None = None) -> list[dict[str, Any]]:
        """Return only orders with a durable, verified incoming payment.

        ``list_orders`` intentionally retains every checkout for address
        scanning, recovery, and late-payment handling. Merchant activity views
        must use this filtered projection so creating a checkout cannot appear
        as a zero-value payment.
        """
        return self.list_merchant_payment_orders_recent(limit)

    def list_merchant_payment_orders_recent(
        self, limit: int | None = MERCHANT_RECENT_LIMIT
    ) -> list[dict[str, Any]]:
        """Return recent verified payments without loading unpaid checkouts."""
        placeholders = ", ".join("?" for _ in self.VERIFIED_PAYMENT_STATUSES)
        query = f"""SELECT o.*, p.name AS product_name, p.price_lamports AS product_price_lamports,
            r.status AS refund_status, r.transaction_signature
            FROM orders o JOIN products p ON p.id = o.product_id
            LEFT JOIN refunds r ON r.order_id = o.id
            WHERE o.payment_signature IS NOT NULL
              AND o.payment_source_address IS NOT NULL
              AND o.payment_lamports IS NOT NULL
              AND o.status IN ({placeholders})
            ORDER BY o.updated_at DESC, o.created_at DESC"""
        parameters: list[Any] = list(self.VERIFIED_PAYMENT_STATUSES)
        if limit is not None:
            query += " LIMIT ?"
            parameters.append(max(1, int(limit)))
        connection = self._connection()
        try:
            rows = connection.execute(query, parameters).fetchall()
            return [self._format_order_row(row) for row in rows]
        finally:
            connection.close()

    @staticmethod
    def _format_order_row(row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        if value.get("price_lamports") is None:
            value["price_lamports"] = value["product_price_lamports"]
        value["price_sol"] = sol_from_lamports(value["price_lamports"])
        value["payment_sol"] = sol_from_lamports(value["payment_lamports"])
        value["payment_status"] = PaymentEngine._payment_status(
            value["status"], value.get("refund_status")
        )
        value["expected_lamports"] = value["price_lamports"]
        value["expected_sol"] = sol_from_lamports(value["price_lamports"])
        value["received_lamports"] = value.get("payment_lamports")
        value["received_sol"] = sol_from_lamports(value.get("payment_lamports"))
        value["sender_address"] = value.get("payment_source_address")
        value["receiving_address"] = value["payment_address"]
        value["payment_timestamp"] = value.get("verified_at") or value.get("updated_at") or value.get("created_at")
        value["review_reason"] = None
        return value

    def merchant_stats(self) -> dict[str, int]:
        """Return dashboard totals from SQLite without scanning Solana."""
        placeholders = ", ".join("?" for _ in self.VERIFIED_PAYMENT_STATUSES)
        connection = self._connection()
        try:
            row = connection.execute(
                f"""WITH verified AS (
                    SELECT o.payment_lamports, COALESCE(o.price_lamports, p.price_lamports) AS price_lamports,
                           r.status AS refund_status
                    FROM orders o
                    JOIN products p ON p.id = o.product_id
                    LEFT JOIN refunds r ON r.order_id = o.id
                    WHERE o.payment_signature IS NOT NULL
                      AND o.payment_source_address IS NOT NULL
                      AND o.payment_lamports IS NOT NULL
                      AND o.status IN ({placeholders})
                )
                SELECT
                    (SELECT COUNT(*) FROM products) AS products,
                    COUNT(*) AS payments,
                    COALESCE(SUM(CASE WHEN payment_lamports > price_lamports
                                      AND (refund_status IS NULL OR refund_status != 'finalized')
                                      THEN 1 ELSE 0 END), 0) AS overpayments,
                    COALESCE(SUM(CASE WHEN refund_status = 'finalized' THEN 1 ELSE 0 END), 0) AS refunded,
                    (SELECT COUNT(*) FROM refunds) AS refunds
                FROM verified""",
                self.VERIFIED_PAYMENT_STATUSES,
            ).fetchone()
            return {key: int(row[key] or 0) for key in row.keys()}
        finally:
            connection.close()

    @staticmethod
    def _payment_status(status: str | None, refund_status: str | None = None) -> str:
        if refund_status == "finalized":
            return "refunded"
        return {
            "awaiting_payment": "pending",
            "payment_pending": "pending",
            "paid_exact": "paid",
            "underpaid": "underpaid",
            "overpaid": "overpaid",
            "refund_processing": "overpaid",
            "refund_submitted": "overpaid",
            "refund_finalized": "refunded",
            "payment_failed": "failed",
            "failed": "failed",
            "needs_review": "needs_review",
        }.get(status or "", "pending")

    @staticmethod
    def _has_verified_payment(order: dict[str, Any]) -> bool:
        """Return whether an order has a durable, verified transfer record."""
        return bool(
            order.get("payment_signature")
            and order.get("payment_source_address")
            and order.get("payment_lamports") is not None
            and order.get("status")
            in {
                "paid_exact",
                "underpaid",
                "overpaid",
                "refund_processing",
                "refund_submitted",
                "refund_finalized",
                "needs_review",
            }
        )

    @staticmethod
    def _verified_payment_status(order: dict[str, Any]) -> str:
        """Classify a verified payment from its amount and refund state."""
        if order.get("refund_status") == "finalized":
            return "refunded"
        amount = int(order["payment_lamports"])
        expected = int(order["price_lamports"])
        if amount < expected:
            return "underpaid"
        if amount > expected:
            return "overpaid"
        return "paid"

    def list_refunds(self, limit: int | None = None) -> list[dict[str, Any]]:
        connection = self._connection()
        try:
            query = """SELECT r.*, o.product_id, o.payment_signature AS original_payment_signature,
                o.payment_address, p.name AS product_name
                FROM refunds r
                JOIN orders o ON o.id = r.order_id
                JOIN products p ON p.id = o.product_id
                ORDER BY r.updated_at DESC"""
            if limit is not None:
                query += " LIMIT ?"
                rows = connection.execute(query, (max(1, int(limit)),)).fetchall()
            else:
                rows = connection.execute(query).fetchall()
            output = []
            for row in rows:
                value = dict(row)
                value["amount_sol"] = sol_from_lamports(value["amount_lamports"])
                value["refund_timestamp"] = value.get("finalized_at") or value.get("updated_at")
                output.append(value)
            return output
        finally:
            connection.close()

    def record_verified_payment(
        self,
        order_id: str,
        verification: PaymentVerification,
        expected_sender: str | None = None,
    ) -> tuple[dict[str, Any], PaymentVerification]:
        """Persist an RPC-verified payment without requiring a customer claim.

        This is an internal merchant refresh path. It still enforces the same
        destination, signature uniqueness, and sender binding rules as the
        customer recovery path; it only removes the claim-code requirement.
        """
        order = self.get_order(order_id)
        if not order:
            return {}, PaymentVerification(
                "needs_review", signature=verification.signature, reason="Checkout order not found"
            )
        if verification.state not in {"exact", "underpayment", "overpayment"}:
            return order, verification
        signature = (verification.signature or "").strip()
        if not signature:
            return order, PaymentVerification(
                "needs_review", reason="Verified payment did not include a signature"
            )
        if expected_sender and verification.source_address != expected_sender:
            return order, PaymentVerification(
                "needs_review", signature=signature, reason="Payment sender did not match the saved sender wallet"
            )
        if (
            order.get("refund_destination_mode") == "internal_demo"
            and order.get("refund_destination") != verification.source_address
        ):
            return order, PaymentVerification(
                "needs_review", signature=signature, reason="The controlled demo wallet was not the verified payment sender"
            )

        connection = self._connection()
        try:
            begin_immediate(connection)
            assigned = connection.execute(
                "SELECT id FROM orders WHERE payment_signature = ?", (signature,)
            ).fetchone()
            if assigned and assigned["id"] != order_id:
                rollback(connection)
                return order, PaymentVerification(
                    "needs_review", signature=signature, reason="This transaction is already assigned to another checkout"
                )
            current = connection.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
            if not current:
                rollback(connection)
                return order, PaymentVerification(
                    "needs_review", signature=signature, reason="Checkout order not found"
                )
            if current["payment_signature"] and current["payment_signature"] != signature:
                rollback(connection)
                return order, PaymentVerification(
                    "needs_review", signature=signature, reason="A different payment is already verified for this checkout"
                )
            status = {
                "exact": "paid_exact",
                "underpayment": "underpaid",
                "overpayment": "overpaid",
            }[verification.state]
            now = iso_now()
            derived_destination = current["refund_destination"] or verification.source_address or ""
            derived_mode = current["refund_destination_mode"]
            if not current["refund_destination"] and verification.source_address:
                derived_mode = "derived_sender"
            connection.execute(
                """UPDATE orders SET status = ?, payment_signature = ?, payment_source_address = ?,
                payment_lamports = ?, refund_destination = ?, refund_destination_mode = ?,
                verified_at = ?, updated_at = ? WHERE id = ?""",
                (
                    status,
                    signature,
                    verification.source_address,
                    verification.amount_lamports,
                    derived_destination,
                    derived_mode,
                    now,
                    now,
                    order_id,
                ),
            )
            commit(connection)
        except sqlite3.IntegrityError:
            rollback(connection)
            return self.get_order(order_id), PaymentVerification(
                "needs_review", signature=signature, reason="This payment could not be assigned safely"
            )
        except Exception:
            rollback(connection)
            raise
        finally:
            connection.close()
        return self.get_order(order_id), verification

    def _wallet_payment_rows(self) -> list[dict[str, Any]]:
        connection = self._connection()
        try:
            rows = connection.execute(
                """SELECT w.*, o.payment_address, o.price_lamports, o.product_id,
                p.name AS product_name
                FROM wallet_sessions w
                JOIN orders o ON o.id = w.order_id
                JOIN products p ON p.id = o.product_id
                WHERE w.payment_signature IS NOT NULL
                ORDER BY w.updated_at DESC"""
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            connection.close()

    def refresh_payment_records(self) -> list[dict[str, Any]]:
        """Refresh checkout payments from finalized Devnet RPC state.

        The refresh is intentionally request-driven: it scans each unique
        checkout address, verifies straightforward native SOL transfers, and
        records only finalized payments. It never broadcasts or asks for a
        recovery claim.
        """
        refresh_overrides: dict[str, dict[str, Any]] = {}
        orders = self.list_orders()

        # A Test Wallet can have a durable signed payment before a customer
        # recovery claim exists. Reconcile those saved signatures first.
        for wallet in self._wallet_payment_rows():
            order = self.get_order(wallet["order_id"])
            if not order or order.get("payment_signature"):
                continue
            signature = wallet["payment_signature"]
            try:
                chain_status = self.solana.signature_status(signature)
            except Exception as error:
                chain_status = "unknown"
                reason = f"Payment status lookup failed: {error}"
            else:
                reason = wallet.get("last_error")
            if chain_status == "finalized":
                try:
                    verification = self.solana.verify_payment(
                        signature, order["payment_address"], order["price_lamports"]
                    )
                except Exception as error:
                    verification = PaymentVerification(
                        "needs_review", signature=signature, reason=f"Payment verification failed: {error}"
                    )
                if verification.state in {"exact", "underpayment", "overpayment"}:
                    self.record_verified_payment(
                        order["id"], verification, expected_sender=wallet["sender_address"]
                    )
                    continue
                reason = verification.reason or reason
            elif chain_status == "failed":
                reason = "The saved payment transaction failed on Solana."
            elif chain_status in {"confirmed", "processed"}:
                reason = "The payment transaction is not finalized yet."
            else:
                reason = reason or "The saved payment signature was not found on Devnet."
            refresh_overrides[order["id"]] = {
                "payment_status": "failed" if chain_status == "failed" or wallet["status"] == "failed" else "needs_review",
                "payment_signature": signature,
                "sender_address": wallet["sender_address"],
                "receiving_address": order["payment_address"],
                "received_lamports": None,
                "review_reason": reason,
                "payment_timestamp": wallet.get("updated_at") or wallet.get("created_at"),
            }

        # Discover customer payments directly from each unique checkout
        # address. Failed and unconfirmed attempts stay in their durable
        # tracking records and never become merchant payment records.
        for order in orders:
            if order.get("payment_signature"):
                try:
                    chain_status = self.solana.signature_status(order["payment_signature"])
                except Exception as error:
                    refresh_overrides[order["id"]] = {
                        "payment_status": "needs_review",
                        "payment_signature": order["payment_signature"],
                        "sender_address": order.get("payment_source_address"),
                        "receiving_address": order["payment_address"],
                        "received_lamports": order.get("payment_lamports"),
                        "review_reason": f"Payment status lookup failed: {error}",
                        "payment_timestamp": order.get("verified_at") or order.get("updated_at"),
                    }
                    continue
                if chain_status == "finalized":
                    try:
                        verification = self.solana.verify_payment(
                            order["payment_signature"], order["payment_address"], order["price_lamports"]
                        )
                    except Exception as error:
                        verification = PaymentVerification(
                            "needs_review", signature=order["payment_signature"], reason=f"Payment verification failed: {error}"
                        )
                    if verification.state in {"exact", "underpayment", "overpayment"}:
                        self.record_verified_payment(order["id"], verification)
                        continue
                    refresh_overrides[order["id"]] = {
                        "payment_status": "failed" if verification.state == "invalid" else "needs_review",
                        "payment_signature": order["payment_signature"],
                        "sender_address": order.get("payment_source_address"),
                        "receiving_address": order["payment_address"],
                        "received_lamports": order.get("payment_lamports"),
                        "review_reason": verification.reason,
                        "payment_timestamp": order.get("verified_at") or order.get("updated_at"),
                    }
                else:
                    refresh_overrides[order["id"]] = {
                        "payment_status": "pending" if chain_status in {"confirmed", "processed"} else "needs_review",
                        "payment_signature": order["payment_signature"],
                        "sender_address": order.get("payment_source_address"),
                        "receiving_address": order["payment_address"],
                        "received_lamports": order.get("payment_lamports"),
                        "review_reason": "The payment transaction is not finalized yet." if chain_status in {"confirmed", "processed"} else "The saved payment signature was not found on Devnet.",
                        "payment_timestamp": order.get("verified_at") or order.get("updated_at"),
                    }
                continue
            try:
                history = self.solana.get_signatures_for_address(order["payment_address"], limit=20)
            except Exception as error:
                refresh_overrides[order["id"]] = {
                    "payment_status": "needs_review",
                    "payment_signature": None,
                    "sender_address": None,
                    "receiving_address": order["payment_address"],
                    "received_lamports": None,
                    "review_reason": f"Payment refresh failed: {error}",
                    "payment_timestamp": order.get("updated_at") or order.get("created_at"),
                }
                continue
            if not history:
                continue
            failed = next((item for item in history if item.get("err") is not None), None)
            candidate = next((item for item in history if item.get("err") is None), None)
            if candidate:
                signature = candidate.get("signature")
                confirmation = candidate.get("confirmationStatus")
                if confirmation in {"confirmed", "finalized"}:
                    try:
                        verification = self.solana.verify_payment(
                            signature, order["payment_address"], order["price_lamports"]
                        )
                    except Exception as error:
                        verification = PaymentVerification(
                            "needs_review", signature=signature, reason=f"Payment verification failed: {error}"
                        )
                    if verification.state in {"exact", "underpayment", "overpayment"} and confirmation == "finalized":
                        self.record_verified_payment(order["id"], verification)
                        continue
                    refresh_overrides[order["id"]] = {
                        "payment_status": "pending" if confirmation != "finalized" else "needs_review",
                        "payment_signature": signature,
                        "sender_address": verification.source_address,
                        "receiving_address": order["payment_address"],
                        "received_lamports": verification.amount_lamports or None,
                        "review_reason": verification.reason or "The payment is not finalized yet.",
                        "payment_timestamp": order.get("updated_at") or order.get("created_at"),
                    }
            elif failed:
                refresh_overrides[order["id"]] = {
                    "payment_status": "failed",
                    "payment_signature": failed.get("signature"),
                    "sender_address": None,
                    "receiving_address": order["payment_address"],
                    "received_lamports": None,
                    "review_reason": "The payment transaction failed on Solana.",
                    "payment_timestamp": failed.get("blockTime") or order.get("updated_at") or order.get("created_at"),
                }

        refreshed = self.list_orders()
        payments = []
        for order in refreshed:
            if not self._has_verified_payment(order):
                continue
            value = dict(order)
            value["expected_lamports"] = order["price_lamports"]
            value["expected_sol"] = sol_from_lamports(order["price_lamports"])
            value["received_lamports"] = order.get("payment_lamports")
            value["received_sol"] = sol_from_lamports(order.get("payment_lamports"))
            value["sender_address"] = order.get("payment_source_address")
            value["receiving_address"] = order["payment_address"]
            value["payment_timestamp"] = order.get("verified_at") or order.get("updated_at") or order.get("created_at")
            value["review_reason"] = None
            value["payment_signature"] = order.get("payment_signature")
            value["payment_status"] = self._verified_payment_status(order)
            value["received_sol"] = sol_from_lamports(value.get("received_lamports"))
            payments.append(value)
        return payments

    def merchant_ledger(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        return self.refresh_payment_records(), self.list_refunds()

    def _authorize(self, order: dict[str, Any] | None, claim_code: str) -> dict[str, Any]:
        if not order or not claim_code or not verify_claim_code(claim_code.strip(), order["claim_code_hash"]):
            raise AuthorizationError()
        return order

    def verify_payment(
        self,
        order_id: str,
        claim_code: str,
        signature: str,
        expected_sender: str | None = None,
    ) -> tuple[dict[str, Any], PaymentVerification]:
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
        if expected_sender and verification.source_address != expected_sender:
            return order, PaymentVerification(
                "invalid",
                signature=signature,
                reason="The payment details could not be verified",
            )
        if (
            order.get("refund_destination_mode") == "internal_demo"
            and order.get("refund_destination") != verification.source_address
        ):
            return order, PaymentVerification(
                "needs_review",
                signature=signature,
                reason="The controlled demo wallet was not the verified payment sender",
            )

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
            derived_destination = current["refund_destination"] or verification.source_address or ""
            derived_mode = current["refund_destination_mode"]
            if not current["refund_destination"] and verification.source_address:
                derived_mode = "derived_sender"
            connection.execute(
                """UPDATE orders SET status = ?, payment_signature = ?, payment_source_address = ?,
                payment_lamports = ?, refund_destination = ?, refund_destination_mode = ?,
                verified_at = ?, updated_at = ? WHERE id = ?""",
                (
                    status,
                    signature,
                    verification.source_address,
                    verification.amount_lamports,
                    derived_destination,
                    derived_mode,
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

    def check_refund_eligibility_by_code(
        self, merchant_code: str, sender_address: str, signature: str
    ) -> tuple[dict[str, Any], str]:
        """Verify recovery using the code, sender, destination, and chain transaction."""
        order = self.get_order_by_merchant_code(merchant_code)
        if not order:
            raise AuthorizationError()
        order, verification = self.verify_payment(
            order["id"], merchant_code, signature, expected_sender=sender_address
        )
        if verification.state in {"invalid", "needs_review"}:
            return order, "not_eligible" if verification.state == "invalid" else "needs_review"
        order = self.get_order(order["id"])
        if order.get("refund_status"):
            return order, order["refund_status"]
        if order["status"] == "overpaid":
            if not order.get("refund_destination") or not order.get("sender_ownership_verified"):
                return order, "needs_review"
            return order, "eligible"
        if order["status"] == "underpaid":
            return order, "underpaid"
        if order["status"] == "paid_exact":
            return order, "exact"
        if order["status"] in {"needs_review", "expired"}:
            return order, "needs_review"
        return order, "not_eligible"

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
            if not order.get("refund_destination") or not order.get("sender_ownership_verified"):
                return order, "needs_review"
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
            return self._create_review_refund(
                order,
                order["excess_lamports"],
                self._eligibility_message(eligibility),
            )
        if eligibility not in {"eligible", "authorized", "broadcast", "submitted", "finalized", "needs_review"}:
            raise DomainError(self._eligibility_message(eligibility), "not_eligible")
        if order.get("refund_id"):
            return self.reconcile_refund(order["refund_id"])
        if not order.get("refund_destination"):
            return self._create_review_refund(
                order,
                order["excess_lamports"],
                "The verified sender could not be prepared as an automatic refund destination; manual review is required.",
            )

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

    def request_refund_by_code(
        self, merchant_code: str, sender_address: str, signature: str
    ) -> tuple[dict[str, Any], str]:
        """Run sender-bound recovery, then reuse the durable refund state machine."""
        order, eligibility = self.check_refund_eligibility_by_code(
            merchant_code, sender_address, signature
        )
        if eligibility == "not_eligible":
            raise DomainError("The payment details could not be verified.", "not_eligible")
        return self.request_refund(order["id"], merchant_code, signature)

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
            destination = order.get("refund_destination") or order.get("payment_source_address") or ""
            connection.execute(
                """INSERT INTO refunds (id, order_id, amount_lamports, destination, status, last_error, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'needs_review', ?, ?, ?)""",
                (refund_id, order["id"], amount, destination, reason, now, now),
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
