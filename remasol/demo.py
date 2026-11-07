from __future__ import annotations

import base64
import sqlite3
import threading
import uuid
from typing import Any

from .db import begin_immediate, commit, connect, rollback
from .security import decrypt_private_key, encrypt_private_key, iso_now, utc_now, verify_claim_code
from .services import AuthorizationError, DomainError, PaymentEngine, lamports_from_sol, sol_from_lamports
from .solana_service import RefundPreparation, SolanaRpcError


class DemoTreasuryService:
    """Small, durable treasury boundary for the public Devnet demo.

    A demo session has one browser-owned buyer wallet, one checkout, one funding
    transfer, and one payment transfer. Signed bytes and signatures are saved
    before every broadcast so retries never construct a second transaction.
    """

    # Only sessions that have reserved treasury-funded work consume an
    # operation slot.  An idle generated wallet is durable state, not an
    # active treasury transfer.  ``needs_review`` remains active because its
    # signed transfer outcome is unresolved and its reservation must not be
    # released or reused.
    ACTIVE_STATUSES = {
        "funding",
        "funding_pending",
        "funded",
        "payment_pending",
        "needs_review",
    }
    TERMINAL_STATUSES = {
        "failed",
        "complete",
        "paid_exact",
        "underpaid",
        "overpaid",
        "refund_processing",
        "refund_submitted",
        "refund_finalized",
    }

    def __init__(
        self,
        db_path: str,
        wallet_encryption_key: str,
        solana,
        engine: PaymentEngine,
        config,
    ):
        self.db_path = db_path
        self.wallet_encryption_key = wallet_encryption_key
        self.solana = solana
        self.engine = engine
        self.config = config
        self._creation_lock = threading.Lock()
        self._execution_lock = threading.Lock()
        self._ensure_treasury()

    def _connection(self):
        return connect(self.db_path)

    def _default_settings(self) -> dict[str, int]:
        return {
            "max_payment_lamports": int(self.config["DEMO_MAX_PAYMENT_LAMPORTS"]),
            "daily_budget_lamports": int(self.config["DEMO_DAILY_BUDGET_LAMPORTS"]),
            "max_concurrent_demos": int(self.config["DEMO_MAX_CONCURRENT_DEMOS"]),
        }

    def _ensure_treasury(self):
        connection = self._connection()
        try:
            if connection.execute("SELECT 1 FROM treasury_settings WHERE id = 1").fetchone():
                return
        finally:
            connection.close()
        if not self.wallet_encryption_key:
            return
        try:
            public_address, private_key = self.solana.generate_wallet()
            encrypted = encrypt_private_key(private_key, self.wallet_encryption_key)
        except Exception:
            return
        defaults = self._default_settings()
        connection = self._connection()
        try:
            begin_immediate(connection)
            if not connection.execute("SELECT 1 FROM treasury_settings WHERE id = 1").fetchone():
                now = iso_now()
                connection.execute(
                    """INSERT INTO treasury_settings
                    (id, public_address, encrypted_private_key, enabled,
                     max_payment_lamports, daily_budget_lamports,
                     max_concurrent_demos, created_at, updated_at)
                    VALUES (1, ?, ?, 1, ?, ?, ?, ?, ?)""",
                    (
                        public_address,
                        encrypted,
                        defaults["max_payment_lamports"],
                        defaults["daily_budget_lamports"],
                        defaults["max_concurrent_demos"],
                        now,
                        now,
                    ),
                )
            commit(connection)
        except Exception:
            rollback(connection)
            raise
        finally:
            connection.close()

    def _settings(self) -> dict[str, Any] | None:
        self._ensure_treasury()
        connection = self._connection()
        try:
            row = connection.execute("SELECT * FROM treasury_settings WHERE id = 1").fetchone()
            return dict(row) if row else None
        finally:
            connection.close()

    def _session(self, browser_id: str | None = None, session_id: str | None = None):
        if not browser_id and not session_id:
            return None
        connection = self._connection()
        try:
            if session_id:
                row = connection.execute(
                    "SELECT * FROM demo_sessions WHERE id = ?", (session_id,)
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT * FROM demo_sessions WHERE browser_id = ?", (browser_id,)
                ).fetchone()
            return dict(row) if row else None
        finally:
            connection.close()

    def _active_count(self, connection) -> int:
        """Count unresolved treasury operations, never idle or terminal wallets."""
        placeholders = ", ".join("?" for _ in self.ACTIVE_STATUSES)
        demo_row = connection.execute(
            f"SELECT COUNT(*) AS count FROM demo_sessions WHERE status IN ({placeholders})",
            tuple(self.ACTIVE_STATUSES),
        ).fetchone()
        wallet_row = connection.execute(
            f"SELECT COUNT(*) AS count FROM wallet_sessions WHERE status IN ({placeholders})",
            tuple(self.ACTIVE_STATUSES),
        ).fetchone()
        return int(demo_row["count"]) + int(wallet_row["count"])

    def _spent_today(self, connection, day_start: str) -> int:
        total = 0
        for table, address_column in (("demo_sessions", "buyer_address"), ("wallet_sessions", "sender_address")):
            row = connection.execute(
                f"""SELECT COALESCE(SUM(funding_lamports), 0) AS amount
                FROM {table}
                WHERE created_at >= ? AND status != 'failed' AND funding_signature IS NOT NULL""",
                (day_start,),
            ).fetchone()
            total += int(row["amount"] or 0)
            failed_rows = connection.execute(
                f"""SELECT funding_lamports, {address_column} AS sender_address
                FROM {table}
                WHERE created_at >= ? AND status = 'failed' AND funding_signature IS NOT NULL""",
                (day_start,),
            ).fetchall()
            for failed in failed_rows:
                funding = int(failed["funding_lamports"] or 0)
                total += min(funding, self._credit_balance(failed["sender_address"], funding))
        return total

    def _funding_buffer_lamports(self) -> int:
        """Keep a just-in-time sender rent-exempt after its payment and fee."""
        fee_buffer = int(self.config["DEMO_FUNDING_FEE_BUFFER_LAMPORTS"])
        getter = getattr(self.solana, "get_minimum_balance_for_rent_exemption", None)
        if not getter:
            return fee_buffer
        try:
            rent_reserve = int(getter(0))
        except Exception:
            rent_reserve = 0
        return max(fee_buffer, rent_reserve + fee_buffer)

    def _public_session(self, row: dict[str, Any] | None) -> dict[str, Any] | None:
        if not row:
            return None
        result = dict(row)
        order = self.engine.get_order(result["order_id"])
        if order:
            order = dict(order)
            order.pop("encrypted_private_key", None)
            order.pop("claim_code_hash", None)
        result["order"] = order
        result["selected_sol"] = sol_from_lamports(result.get("selected_lamports"))
        result["funding_sol"] = sol_from_lamports(result.get("funding_lamports"))
        try:
            result["buyer_balance_lamports"] = self.solana.get_balance(result["buyer_address"])
        except Exception:
            result["buyer_balance_lamports"] = None
        result["buyer_balance_sol"] = sol_from_lamports(result.get("buyer_balance_lamports"))
        result.pop("encrypted_private_key", None)
        result.pop("funding_signed_transaction", None)
        result.pop("payment_signed_transaction", None)
        return result

    def _wallet_session(self, browser_id: str | None = None, session_id: str | None = None):
        if not browser_id and not session_id:
            return None
        connection = self._connection()
        try:
            if session_id:
                if browser_id:
                    row = connection.execute(
                        "SELECT * FROM wallet_sessions WHERE id = ? AND browser_id = ?",
                        (session_id, browser_id),
                    ).fetchone()
                else:
                    row = connection.execute(
                        "SELECT * FROM wallet_sessions WHERE id = ?", (session_id,)
                    ).fetchone()
            else:
                row = connection.execute(
                    """SELECT * FROM wallet_sessions WHERE browser_id = ?
                    ORDER BY created_at DESC LIMIT 1""",
                    (browser_id,),
                ).fetchone()
            return dict(row) if row else None
        finally:
            connection.close()

    def _public_wallet_session(self, row: dict[str, Any] | None) -> dict[str, Any] | None:
        if not row:
            return None
        result = dict(row)
        try:
            result["available_balance_lamports"] = self.solana.get_balance(result["sender_address"])
        except Exception:
            result["available_balance_lamports"] = None
        result["available_balance_sol"] = sol_from_lamports(result.get("available_balance_lamports"))
        result["selected_sol"] = sol_from_lamports(result.get("selected_lamports"))
        result["funding_sol"] = sol_from_lamports(result.get("funding_lamports"))
        result["actual_balance_available"] = result.get("available_balance_lamports") is not None
        result.pop("encrypted_private_key", None)
        result.pop("funding_signed_transaction", None)
        result.pop("payment_signed_transaction", None)
        return result

    def _wallet_history(self, browser_id: str) -> list[dict[str, Any]]:
        connection = self._connection()
        try:
            rows = connection.execute(
                """SELECT * FROM wallet_sessions
                WHERE browser_id = ? ORDER BY updated_at DESC, created_at DESC""",
                (browser_id,),
            ).fetchall()
        finally:
            connection.close()
        return [self._public_wallet_session(dict(row)) for row in rows]

    def _credit_balance(self, address: str, fallback: int) -> int:
        """Read a wallet balance conservatively for allowance accounting."""
        try:
            return max(0, int(self.solana.get_balance(address)))
        except Exception:
            # An unavailable RPC must never make locked funds look releasable.
            return fallback

    def _credit_accounting(
        self, settings: dict[str, Any] | None, day_start: str | None = None
    ) -> dict[str, int]:
        """Return the allowance ledger used by both the UI and reservations.

        A failed session is released from the daily allowance only when its
        sender no longer holds the allocation. If the balance cannot be read,
        the full saved funding amount remains reserved. This is intentionally
        conservative for unresolved Devnet outcomes.
        """
        if not settings:
            return {
                "successful_allocations_lamports": 0,
                "unrecovered_funds_lamports": 0,
                "releaseable_failed_lamports": 0,
            }
        if day_start is None:
            now = utc_now()
            day_start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()

        successful_allocations = 0
        unrecovered_funds = 0
        releaseable_failed = 0
        connection = self._connection()
        try:
            for table, address_column in (("demo_sessions", "buyer_address"), ("wallet_sessions", "sender_address")):
                rows = connection.execute(
                    f"""SELECT status, funding_lamports, funding_signature,
                    {address_column} AS sender_address
                    FROM {table} WHERE created_at >= ?""",
                    (day_start,),
                ).fetchall()
                for row in rows:
                    funding = int(row["funding_lamports"] or 0)
                    if not funding or not row["funding_signature"]:
                        continue
                    if row["status"] != "failed":
                        successful_allocations += funding
                        continue

                    locked = min(funding, self._credit_balance(row["sender_address"], funding))
                    unrecovered_funds += locked
                    successful_allocations += locked
                    releaseable_failed += max(0, funding - locked)
        finally:
            connection.close()
        return {
            "successful_allocations_lamports": successful_allocations,
            "unrecovered_funds_lamports": unrecovered_funds,
            "releaseable_failed_lamports": releaseable_failed,
        }

    def wallet_page_state(
        self,
        browser_id: str,
        session_id: str | None = None,
        *,
        include_terminal: bool = False,
    ) -> dict[str, Any]:
        settings = self._settings()
        # The entry page is deliberately session-independent. A wallet can be
        # selected only by an explicit, browser-owned /wallet/test/<id> URL.
        row = (
            self._wallet_session(browser_id=browser_id, session_id=session_id)
            if session_id
            else None
        )
        if row and row["status"] in self.TERMINAL_STATUSES and not include_terminal:
            # Terminal sessions are history, never an automatically restored
            # active test. An explicit test URL may inspect the saved result.
            row = None
        # Keep treasury accounting available to internal callers and the
        # merchant view, but do not turn it into a visitor-facing allowance.
        accounting = self._credit_accounting(settings)
        return {
            "wallet": self._public_wallet_session(row),
            "treasury_enabled": bool(settings and settings["enabled"]),
            "treasury_configured": bool(settings),
            "credit_accounting": {
                key: sol_from_lamports(value) for key, value in accounting.items()
            },
            "max_payment_sol": sol_from_lamports(
                min(
                    settings["max_payment_lamports"] if settings else self.config["DEMO_MAX_PAYMENT_LAMPORTS"],
                    self.config["DEMO_MAX_PAYMENT_LAMPORTS"],
                )
            ),
            "min_payment_sol": sol_from_lamports(self.config["DEMO_MIN_PAYMENT_LAMPORTS"]),
        }

    def create_test_wallet(self, browser_id: str, client_ip: str) -> dict[str, Any]:
        with self._creation_lock:
            settings = self._settings()
            if not settings:
                raise DomainError("The demo treasury is not configured yet.", "configuration")
            if not settings["enabled"]:
                raise DomainError("The test wallet is paused by the merchant. Try again later.", "paused")
            if not self.wallet_encryption_key:
                raise DomainError("The test wallet is not configured yet.", "configuration")
            sender_address, sender_private_key = self.solana.generate_wallet()
            now = iso_now()
            wallet_id = str(uuid.uuid4())
            connection = self._connection()
            try:
                begin_immediate(connection)
                current_settings = connection.execute(
                    "SELECT enabled FROM treasury_settings WHERE id = 1"
                ).fetchone()
                if not current_settings or not current_settings["enabled"]:
                    rollback(connection)
                    raise DomainError("The test wallet is paused by the merchant. Try again later.", "paused")
                connection.execute(
                    """INSERT INTO wallet_sessions
                    (id, browser_id, sender_address, encrypted_private_key, client_ip,
                     status, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, 'created', ?, ?)""",
                    (
                        wallet_id,
                        browser_id,
                        sender_address,
                        encrypt_private_key(sender_private_key, self.wallet_encryption_key),
                        client_ip,
                        now,
                        now,
                    ),
                )
                commit(connection)
            except sqlite3.IntegrityError as error:
                rollback(connection)
                raise DomainError("A new test wallet could not be created. Try again.", "create_failed") from error
            except Exception:
                rollback(connection)
                raise
            finally:
                connection.close()
            return self._public_wallet_session(self._wallet_session(session_id=wallet_id))

    def _active_wallet_recipient(self, recipient_address: str) -> dict[str, Any]:
        recipient_address = (recipient_address or "").strip()
        if not self.solana.validate_address(recipient_address):
            raise DomainError("Paste an active RemaSol checkout address.", "recipient")
        order = self.engine.get_active_checkout_by_payment_address(recipient_address)
        if not order:
            raise DomainError("Paste an active RemaSol checkout address.", "recipient")
        return order

    def page_state(self, browser_id: str) -> dict[str, Any]:
        row = self._session(browser_id=browser_id)
        settings = self._settings()
        return {
            "demo": self._public_session(row),
            "treasury_enabled": bool(settings and settings["enabled"]),
            "treasury_configured": bool(settings),
            "max_payment_sol": sol_from_lamports(
                min(
                    settings["max_payment_lamports"] if settings else self.config["DEMO_MAX_PAYMENT_LAMPORTS"],
                    self.config["DEMO_MAX_PAYMENT_LAMPORTS"],
                )
            ),
            "min_payment_sol": sol_from_lamports(self.config["DEMO_MIN_PAYMENT_LAMPORTS"]),
        }

    def create_demo(self, browser_id: str, client_ip: str) -> tuple[dict[str, Any], str]:
        with self._creation_lock:
            existing = self._session(browser_id=browser_id)
            if existing:
                raise DomainError(
                    "This browser already has a demo session. Continue with the existing wallet below.",
                    "existing_demo",
                )
            settings = self._settings()
            if not settings:
                raise DomainError("The demo treasury is not configured yet.", "configuration")
            if not settings["enabled"]:
                raise DomainError("The public demo is paused by the merchant. Try again later.", "paused")
            connection = self._connection()
            try:
                begin_immediate(connection)
                commit(connection)
            except Exception:
                rollback(connection)
                raise
            finally:
                connection.close()

            product = self.engine.get_product(self.config["DEMO_PRODUCT_SLUG"])
            if not product:
                raise DomainError("The demo ebook is not configured.", "configuration")
            buyer_address, buyer_private_key = self.solana.generate_wallet()
            order, merchant_code = self.engine.create_demo_order(
                product["slug"], buyer_address, client_ip
            )
            now = iso_now()
            demo_id = __import__("uuid").uuid4().hex
            connection = self._connection()
            try:
                connection.execute(
                    """INSERT INTO demo_sessions
                    (id, browser_id, order_id, buyer_address, encrypted_private_key,
                     status, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, 'created', ?, ?)""",
                    (
                        demo_id,
                        browser_id,
                        order["id"],
                        buyer_address,
                        encrypt_private_key(buyer_private_key, self.wallet_encryption_key),
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise DomainError("This browser already has a demo session.", "existing_demo") from error
            finally:
                connection.close()
            return self._public_session(self._session(session_id=demo_id)), merchant_code

    def _parse_amount(self, value: str, settings: dict[str, Any]) -> int:
        try:
            amount = lamports_from_sol(value)
        except DomainError as error:
            raise DomainError("Enter a payment amount between 0.0005 and 0.02 SOL.", "validation") from error
        maximum = min(settings["max_payment_lamports"], self.config["DEMO_MAX_PAYMENT_LAMPORTS"])
        minimum = self.config["DEMO_MIN_PAYMENT_LAMPORTS"]
        if amount < minimum or amount > maximum:
            raise DomainError(
                f"Enter a payment amount between {sol_from_lamports(minimum)} and {sol_from_lamports(maximum)} SOL.",
                "validation",
            )
        return amount

    def _reserve_demo(self, demo_id: str, selected_lamports: int, settings: dict[str, Any]):
        now = utc_now()
        funding_lamports = selected_lamports + self._funding_buffer_lamports()
        connection = self._connection()
        try:
            begin_immediate(connection)
            row = connection.execute("SELECT * FROM demo_sessions WHERE id = ?", (demo_id,)).fetchone()
            if not row:
                rollback(connection)
                raise DomainError("That demo session no longer exists.", "not_found")
            row = dict(row)
            if row["selected_lamports"] is not None and row["selected_lamports"] != selected_lamports:
                rollback(connection)
                raise DomainError("This demo payment is already reserved. Use the existing amount.", "locked")
            if row["status"] in self.TERMINAL_STATUSES:
                rollback(connection)
                return row
            if not settings["enabled"]:
                rollback(connection)
                raise DomainError("The public demo is paused by the merchant. Try again later.", "paused")
            active = self._active_count(connection)
            if row["status"] == "created" and active >= settings["max_concurrent_demos"]:
                rollback(connection)
                raise DomainError("Treasury-funded test operations are currently at capacity. Try again later.", "capacity")
            day_start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
            if self._spent_today(connection, day_start) + funding_lamports > settings["daily_budget_lamports"]:
                rollback(connection)
                raise DomainError("The daily demo treasury budget is exhausted. Try again later.", "budget")
            connection.execute(
                """UPDATE demo_sessions SET selected_lamports = ?, funding_lamports = ?,
                status = CASE WHEN status = 'created' THEN 'funding' ELSE status END,
                last_submit_at = ?, last_error = NULL, updated_at = ? WHERE id = ?""",
                (selected_lamports, funding_lamports, now.isoformat(), now.isoformat(), demo_id),
            )
            commit(connection)
            return self._session(session_id=demo_id)
        except Exception:
            rollback(connection)
            raise
        finally:
            connection.close()

    def _save_transfer(self, demo_id: str, kind: str, preparation: RefundPreparation):
        connection = self._connection()
        try:
            connection.execute(
                f"""UPDATE demo_sessions SET {kind}_signed_transaction = ?, {kind}_signature = ?,
                status = ?, updated_at = ? WHERE id = ? AND {kind}_signature IS NULL""",
                (
                    base64.b64encode(preparation.signed_transaction).decode(),
                    preparation.transaction_signature,
                    "funding_pending" if kind == "funding" else "payment_pending",
                    iso_now(),
                    demo_id,
                ),
            )
        finally:
            connection.close()

    def _mark_demo(self, demo_id: str, status: str, error: str | None = None):
        connection = self._connection()
        try:
            connection.execute(
                "UPDATE demo_sessions SET status = ?, last_error = ?, updated_at = ? WHERE id = ?",
                (status, error, iso_now(), demo_id),
            )
        finally:
            connection.close()

    def _finalized(self, signature: str) -> bool:
        try:
            return self.solana.signature_status(signature) == "finalized"
        except Exception:
            return False

    def _execute_funding(self, row: dict[str, Any]) -> dict[str, Any]:
        demo_id = row["id"]
        if row.get("status") == "needs_review":
            return row
        treasury = self._settings()
        if not treasury:
            self._mark_demo(demo_id, "failed", "The demo treasury is unavailable.")
            return self._session(session_id=demo_id)
        if not row.get("funding_signature"):
            try:
                if self.solana.get_balance(treasury["public_address"]) < row["funding_lamports"]:
                    self._mark_demo(demo_id, "failed", "The demo treasury needs more Devnet SOL.")
                    return self._session(session_id=demo_id)
                private_key = decrypt_private_key(
                    treasury["encrypted_private_key"], self.wallet_encryption_key
                )
                preparation = self.solana.prepare_transfer(
                    private_key, row["buyer_address"], row["funding_lamports"]
                )
                self._save_transfer(demo_id, "funding", preparation)
                row = self._session(session_id=demo_id)
                try:
                    broadcast = self.solana.broadcast_signed_transaction(preparation.signed_transaction)
                except Exception as error:
                    if self._finalized(preparation.transaction_signature):
                        self._mark_demo(demo_id, "funded")
                    else:
                        self._mark_demo(demo_id, "needs_review", f"Funding outcome is uncertain: {error}")
                    return self._session(session_id=demo_id)
                if broadcast != preparation.transaction_signature:
                    self._mark_demo(demo_id, "needs_review", "Funding RPC returned a different signature.")
                    return self._session(session_id=demo_id)
            except Exception as error:
                self._mark_demo(demo_id, "failed", f"Demo funding could not be prepared: {error}")
                return self._session(session_id=demo_id)
        row = self._session(session_id=demo_id)
        if row.get("status") == "needs_review":
            return row
        if not self._finalized(row["funding_signature"]):
            return row
        self._mark_demo(demo_id, "funded")
        return self._session(session_id=demo_id)

    def _execute_payment(self, row: dict[str, Any], merchant_code: str) -> tuple[dict[str, Any], Any]:
        demo_id = row["id"]
        order = self.engine.get_order(row["order_id"])
        if row.get("status") == "needs_review":
            return row, None
        if not row.get("payment_signature"):
            try:
                buyer_key = decrypt_private_key(row["encrypted_private_key"], self.wallet_encryption_key)
                preparation = self.solana.prepare_transfer(
                    buyer_key, order["payment_address"], row["selected_lamports"]
                )
                self._save_transfer(demo_id, "payment", preparation)
                try:
                    broadcast = self.solana.broadcast_signed_transaction(preparation.signed_transaction)
                except Exception as error:
                    if not self._finalized(preparation.transaction_signature):
                        self._mark_demo(demo_id, "needs_review", f"Payment outcome is uncertain: {error}")
                        return self._session(session_id=demo_id), None
                    broadcast = preparation.transaction_signature
                if broadcast != preparation.transaction_signature:
                    self._mark_demo(demo_id, "needs_review", "Payment RPC returned a different signature.")
                    return self._session(session_id=demo_id), None
            except Exception as error:
                self._mark_demo(demo_id, "needs_review", f"Demo payment could not be prepared: {error}")
                return self._session(session_id=demo_id), None
        row = self._session(session_id=demo_id)
        if not self._finalized(row["payment_signature"]):
            return row, None
        order, verification = self.engine.verify_payment(
            row["order_id"], merchant_code, row["payment_signature"], expected_sender=row["buyer_address"]
        )
        if verification.state in {"exact", "underpayment", "overpayment"}:
            status = {
                "exact": "paid_exact",
                "underpayment": "underpaid",
                "overpayment": "overpaid",
            }[verification.state]
            self._mark_demo(demo_id, status)
        else:
            self._mark_demo(demo_id, "needs_review", verification.reason)
        return self._session(session_id=demo_id), verification

    def run_demo(
        self, browser_id: str, merchant_code: str, amount: str, client_ip: str
    ) -> tuple[dict[str, Any], Any]:
        row = self._session(browser_id=browser_id)
        if not row:
            raise DomainError("Create a demo wallet before sending test SOL.", "not_found")
        order = self.engine.get_order(row["order_id"])
        if not order or not verify_claim_code(merchant_code.strip(), order["claim_code_hash"]):
            raise AuthorizationError()
        settings = self._settings()
        if not settings:
            raise DomainError("The demo treasury is not configured yet.", "configuration")
        if row["status"] in self.TERMINAL_STATUSES:
            return self._public_session(row), None
        selected = self._parse_amount(amount, settings)
        row = self._reserve_demo(row["id"], selected, settings)
        with self._execution_lock:
            row = self._session(session_id=row["id"])
            if row["status"] in self.TERMINAL_STATUSES:
                return self._public_session(row), None
            row = self._execute_funding(row)
            if row.get("status") in {"failed", "needs_review"}:
                return self._public_session(row), None
            if not self._finalized(row.get("funding_signature", "")):
                return self._public_session(row), None
            row, verification = self._execute_payment(row, merchant_code)
        return self._public_session(row), verification

    def _reserve_wallet(
        self,
        wallet_id: str,
        recipient_address: str,
        order_id: str,
        selected_lamports: int,
        settings: dict[str, Any],
    ) -> dict[str, Any]:
        now = utc_now()
        funding_lamports = selected_lamports + self._funding_buffer_lamports()
        connection = self._connection()
        try:
            begin_immediate(connection)
            row = connection.execute(
                "SELECT * FROM wallet_sessions WHERE id = ?", (wallet_id,)
            ).fetchone()
            if not row:
                rollback(connection)
                raise DomainError("That test wallet no longer exists.", "not_found")
            row = dict(row)
            if row["selected_lamports"] is not None:
                if (
                    row["selected_lamports"] != selected_lamports
                    or row["recipient_address"] != recipient_address
                    or row["order_id"] != order_id
                ):
                    rollback(connection)
                    raise DomainError(
                        "This test payment is already reserved. Use the existing recipient and amount.",
                        "locked",
                    )
            if row["status"] in self.TERMINAL_STATUSES:
                rollback(connection)
                return row
            if not settings["enabled"]:
                rollback(connection)
                raise DomainError("The test wallet is paused by the merchant. Try again later.", "paused")
            active = self._active_count(connection)
            if row["status"] == "created" and active >= settings["max_concurrent_demos"]:
                rollback(connection)
                raise DomainError(
                    "Treasury-funded test operations are currently at capacity. Try again later.",
                    "capacity",
                )
            day_start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
            if self._spent_today(connection, day_start) + funding_lamports > settings["daily_budget_lamports"]:
                rollback(connection)
                raise DomainError("The daily demo treasury budget is exhausted. Try again later.", "budget")
            # Lock the generated sender to this checkout before reserving the
            # wallet or allowing the treasury funding path to broadcast. The
            # service refuses to overwrite any pre-existing destination.
            self.engine.bind_demo_refund_destination(
                order_id,
                row["sender_address"],
                connection=connection,
            )
            connection.execute(
                """UPDATE wallet_sessions SET recipient_address = ?, order_id = ?,
                selected_lamports = ?, funding_lamports = ?,
                status = CASE WHEN status = 'created' THEN 'funding' ELSE status END,
                last_submit_at = ?, last_error = NULL, updated_at = ? WHERE id = ?""",
                (
                    recipient_address,
                    order_id,
                    selected_lamports,
                    funding_lamports,
                    now.isoformat(),
                    now.isoformat(),
                    wallet_id,
                ),
            )
            commit(connection)
            return self._wallet_session(session_id=wallet_id)
        except Exception:
            rollback(connection)
            raise
        finally:
            connection.close()

    def _save_wallet_transfer(self, wallet_id: str, kind: str, preparation: RefundPreparation):
        connection = self._connection()
        try:
            connection.execute(
                f"""UPDATE wallet_sessions SET {kind}_signed_transaction = ?, {kind}_signature = ?,
                status = ?, updated_at = ? WHERE id = ? AND {kind}_signature IS NULL""",
                (
                    base64.b64encode(preparation.signed_transaction).decode(),
                    preparation.transaction_signature,
                    "funding_pending" if kind == "funding" else "payment_pending",
                    iso_now(),
                    wallet_id,
                ),
            )
        finally:
            connection.close()

    def _mark_wallet(self, wallet_id: str, status: str, error: str | None = None):
        connection = self._connection()
        try:
            connection.execute(
                "UPDATE wallet_sessions SET status = ?, last_error = ?, updated_at = ? WHERE id = ?",
                (status, error, iso_now(), wallet_id),
            )
        finally:
            connection.close()

    def _execute_wallet_funding(self, row: dict[str, Any]) -> dict[str, Any]:
        wallet_id = row["id"]
        if row.get("status") == "needs_review":
            return row
        treasury = self._settings()
        if not treasury:
            self._mark_wallet(wallet_id, "failed", "The demo treasury is unavailable.")
            return self._wallet_session(session_id=wallet_id)
        if not row.get("funding_signature"):
            try:
                if self.solana.get_balance(treasury["public_address"]) < row["funding_lamports"]:
                    self._mark_wallet(wallet_id, "failed", "The demo treasury needs more Devnet SOL.")
                    return self._wallet_session(session_id=wallet_id)
                private_key = decrypt_private_key(
                    treasury["encrypted_private_key"], self.wallet_encryption_key
                )
                preparation = self.solana.prepare_transfer(
                    private_key, row["sender_address"], row["funding_lamports"]
                )
                self._save_wallet_transfer(wallet_id, "funding", preparation)
                try:
                    broadcast = self.solana.broadcast_signed_transaction(preparation.signed_transaction)
                except Exception as error:
                    if self._finalized(preparation.transaction_signature):
                        self._mark_wallet(wallet_id, "funded")
                    else:
                        self._mark_wallet(wallet_id, "needs_review", f"Funding outcome is uncertain: {error}")
                    return self._wallet_session(session_id=wallet_id)
                if broadcast != preparation.transaction_signature:
                    self._mark_wallet(wallet_id, "needs_review", "Funding RPC returned a different signature.")
                    return self._wallet_session(session_id=wallet_id)
            except Exception as error:
                self._mark_wallet(wallet_id, "failed", f"Test wallet funding could not be prepared: {error}")
                return self._wallet_session(session_id=wallet_id)
        row = self._wallet_session(session_id=wallet_id)
        if row.get("status") == "needs_review":
            return row
        if not self._finalized(row.get("funding_signature", "")):
            return row
        self._mark_wallet(wallet_id, "funded")
        return self._wallet_session(session_id=wallet_id)

    def _execute_wallet_payment(self, row: dict[str, Any]) -> dict[str, Any]:
        wallet_id = row["id"]
        if row.get("status") == "needs_review":
            return row
        order = self._active_wallet_recipient(row.get("recipient_address", ""))
        if order["id"] != row.get("order_id"):
            self._mark_wallet(wallet_id, "needs_review", "The checkout recipient could not be verified safely.")
            return self._wallet_session(session_id=wallet_id)
        if not row.get("payment_signature"):
            try:
                sender_key = decrypt_private_key(row["encrypted_private_key"], self.wallet_encryption_key)
                preparation = self.solana.prepare_transfer(
                    sender_key, row["recipient_address"], row["selected_lamports"]
                )
                self._save_wallet_transfer(wallet_id, "payment", preparation)
                try:
                    broadcast = self.solana.broadcast_signed_transaction(preparation.signed_transaction)
                except Exception as error:
                    if not self._finalized(preparation.transaction_signature):
                        self._mark_wallet(wallet_id, "needs_review", f"Payment outcome is uncertain: {error}")
                        return self._wallet_session(session_id=wallet_id)
                    broadcast = preparation.transaction_signature
                if broadcast != preparation.transaction_signature:
                    self._mark_wallet(wallet_id, "needs_review", "Payment RPC returned a different signature.")
                    return self._wallet_session(session_id=wallet_id)
            except Exception as error:
                self._mark_wallet(wallet_id, "needs_review", f"Test payment could not be prepared: {error}")
                return self._wallet_session(session_id=wallet_id)
        row = self._wallet_session(session_id=wallet_id)
        if row.get("status") == "needs_review":
            return row
        if not self._finalized(row.get("payment_signature", "")):
            return row
        try:
            verification = self.solana.verify_payment(
                row["payment_signature"], row["recipient_address"], row["selected_lamports"]
            )
            if verification.state in {"exact", "underpayment", "overpayment"}:
                _order, recorded = self.engine.record_verified_payment(
                    row["order_id"], verification, expected_sender=row["sender_address"]
                )
                if recorded.state not in {"exact", "underpayment", "overpayment"}:
                    self._mark_wallet(wallet_id, "needs_review", recorded.reason)
                else:
                    self._mark_wallet(wallet_id, "complete")
            else:
                self._mark_wallet(wallet_id, "needs_review", verification.reason)
        except Exception as error:
            self._mark_wallet(wallet_id, "needs_review", f"Payment verification failed: {error}")
        return self._wallet_session(session_id=wallet_id)

    def reconcile_test_wallet_payment(
        self, browser_id: str, session_id: str | None = None
    ) -> dict[str, Any]:
        """Recheck the saved payment signature without constructing a transfer."""
        if not session_id:
            raise DomainError("Open a saved Test Wallet session before checking payment status.", "not_found")
        row = self._wallet_session(browser_id=browser_id, session_id=session_id)
        if not row:
            raise DomainError("Create a test wallet before checking payment status.", "not_found")
        if not row.get("payment_signature"):
            raise DomainError("There is no saved payment signature to recheck.", "not_found")
        if row["status"] in {"complete", "failed"}:
            return self._public_wallet_session(row)
        signature = row["payment_signature"]
        try:
            chain_status = self.solana.signature_status(signature)
        except Exception as error:
            self._mark_wallet(row["id"], "needs_review", f"Payment status lookup failed: {error}")
            return self._public_wallet_session(self._wallet_session(session_id=row["id"]))
        if chain_status == "failed":
            self._mark_wallet(row["id"], "failed", "The saved payment transaction failed on Solana.")
        elif chain_status in {"confirmed", "finalized"}:
            try:
                verification = self.solana.verify_payment(
                    signature, row["recipient_address"], row["selected_lamports"]
                )
                if verification.state in {"exact", "underpayment", "overpayment"} and chain_status == "finalized":
                    _order, recorded = self.engine.record_verified_payment(
                        row["order_id"], verification, expected_sender=row["sender_address"]
                    )
                    if recorded.state in {"exact", "underpayment", "overpayment"}:
                        self._mark_wallet(row["id"], "complete")
                    else:
                        self._mark_wallet(row["id"], "needs_review", recorded.reason)
                elif verification.state in {"exact", "underpayment", "overpayment"}:
                    self._mark_wallet(row["id"], "payment_pending", "Payment is confirmed but not finalized yet.")
                else:
                    self._mark_wallet(row["id"], "needs_review", verification.reason)
            except Exception as error:
                self._mark_wallet(row["id"], "needs_review", f"Payment verification failed: {error}")
        elif chain_status in {"processed"}:
            self._mark_wallet(row["id"], "payment_pending", "Payment is still being confirmed on Devnet.")
        else:
            self._mark_wallet(
                row["id"],
                "needs_review",
                "The saved payment signature was not found on Devnet. No duplicate payment was created.",
            )
        return self._public_wallet_session(self._wallet_session(session_id=row["id"]))

    def send_test_wallet_payment(
        self,
        browser_id: str,
        recipient_address: str,
        amount: str,
        client_ip: str,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        if not session_id:
            raise DomainError("Open a New Test before sending test SOL.", "not_found")
        row = self._wallet_session(browser_id=browser_id, session_id=session_id)
        if not row:
            raise DomainError("Create a test wallet before sending test SOL.", "not_found")
        if row["status"] == "needs_review":
            raise DomainError(
                "This payment needs review. Recheck the saved payment status before trying anything else.",
                "needs_review",
            )
        if row["status"] in self.TERMINAL_STATUSES:
            return self._public_wallet_session(row)
        order = self._active_wallet_recipient(recipient_address)
        settings = self._settings()
        if not settings:
            raise DomainError("The demo treasury is not configured yet.", "configuration")
        selected = self._parse_amount(amount, settings)
        row = self._reserve_wallet(row["id"], recipient_address.strip(), order["id"], selected, settings)
        with self._execution_lock:
            row = self._wallet_session(session_id=row["id"])
            if row["status"] in self.TERMINAL_STATUSES:
                return self._public_wallet_session(row)
            row = self._execute_wallet_funding(row)
            if row.get("status") in {"failed", "needs_review"}:
                return self._public_wallet_session(row)
            if not self._finalized(row.get("funding_signature", "")):
                return self._public_wallet_session(row)
            row = self._execute_wallet_payment(row)
        return self._public_wallet_session(row)

    def treasury_dashboard(self) -> dict[str, Any]:
        settings = self._settings()
        if not settings:
            return {"configured": False}
        now = utc_now()
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        credit_accounting = self._credit_accounting(settings, day_start)
        connection = self._connection()
        try:
            counts = sum(
                connection.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()["count"]
                for table in ("demo_sessions", "wallet_sessions")
            )
            active = self._active_count(connection)
            spent_today = self._spent_today(connection, day_start)
            transferred = sum(
                connection.execute(
                    f"SELECT COALESCE(SUM(selected_lamports), 0) AS amount FROM {table} WHERE payment_signature IS NOT NULL"
                ).fetchone()["amount"]
                for table in ("demo_sessions", "wallet_sessions")
            )
            unused_buffer = sum(
                connection.execute(
                    f"""SELECT COALESCE(SUM(CASE WHEN funding_lamports > selected_lamports
                    THEN funding_lamports - selected_lamports ELSE 0 END), 0) AS amount
                    FROM {table} WHERE status != 'failed'"""
                ).fetchone()["amount"]
                for table in ("demo_sessions", "wallet_sessions")
            )
            refunds = connection.execute(
                """SELECT COALESCE(SUM(r.amount_lamports), 0) AS amount
                FROM refunds r
                WHERE r.order_id IN (
                    SELECT order_id FROM demo_sessions
                    UNION
                    SELECT order_id FROM wallet_sessions
                )"""
            ).fetchone()["amount"]
            rows = connection.execute(
                """SELECT d.*, o.payment_address, o.product_id
                FROM demo_sessions d JOIN orders o ON o.id = d.order_id
                ORDER BY d.updated_at DESC LIMIT 12"""
            ).fetchall()
        finally:
            connection.close()
        try:
            balance = self.solana.get_balance(settings["public_address"])
        except Exception:
            balance = None
        recent = []
        for row in rows:
            value = dict(row)
            value.pop("encrypted_private_key", None)
            value.pop("funding_signed_transaction", None)
            value.pop("payment_signed_transaction", None)
            value["funding_sol"] = sol_from_lamports(value.get("funding_lamports"))
            value["selected_sol"] = sol_from_lamports(value.get("selected_lamports"))
            recent.append(value)
        return {
            "configured": True,
            "public_address": settings["public_address"],
            "enabled": bool(settings["enabled"]),
            "max_payment_sol": sol_from_lamports(settings["max_payment_lamports"]),
            "daily_budget_sol": sol_from_lamports(settings["daily_budget_lamports"]),
            "max_concurrent_demos": settings["max_concurrent_demos"],
            "balance_lamports": balance,
            "balance_sol": sol_from_lamports(balance),
            "total_sessions": counts,
            "active_sessions": active,
            "total_transferred_sol": sol_from_lamports(transferred),
            "unused_funding_sol": sol_from_lamports(unused_buffer),
            "total_refunds_sol": sol_from_lamports(refunds),
            "spent_today_lamports": spent_today,
            "remaining_daily_budget_sol": sol_from_lamports(
                max(0, settings["daily_budget_lamports"] - int(spent_today or 0))
            ),
            "credit_accounting": {
                key: sol_from_lamports(value) for key, value in credit_accounting.items()
            },
            "recent": recent,
        }

    def update_settings(
        self, enabled: bool, max_payment: str, daily_budget: str, max_concurrent: str
    ) -> dict[str, Any]:
        settings = self._settings()
        if not settings:
            raise DomainError("The demo treasury is not configured yet.", "configuration")
        try:
            max_payment_lamports = lamports_from_sol(max_payment)
            daily_budget_lamports = lamports_from_sol(daily_budget)
            max_concurrent_demos = int(max_concurrent)
        except (DomainError, TypeError, ValueError) as error:
            raise DomainError("Use valid SOL limits and a whole-number concurrency limit.", "validation") from error
        if not self.config["DEMO_MIN_PAYMENT_LAMPORTS"] <= max_payment_lamports <= self.config["DEMO_MAX_PAYMENT_LAMPORTS"]:
            raise DomainError("Maximum demo payment must stay between 0.0005 and 0.02 SOL.", "validation")
        if daily_budget_lamports < max_payment_lamports or max_concurrent_demos < 1 or max_concurrent_demos > 100:
            raise DomainError("Daily budget must cover one demo and concurrency must be between 1 and 100.", "validation")
        connection = self._connection()
        try:
            connection.execute(
                """UPDATE treasury_settings SET enabled = ?, max_payment_lamports = ?,
                daily_budget_lamports = ?, max_concurrent_demos = ?, updated_at = ? WHERE id = 1""",
                (1 if enabled else 0, max_payment_lamports, daily_budget_lamports, max_concurrent_demos, iso_now()),
            )
        finally:
            connection.close()
        return self.treasury_dashboard()
