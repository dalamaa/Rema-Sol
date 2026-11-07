from __future__ import annotations

import base64
import asyncio
import json
from dataclasses import dataclass
from typing import Any


class SolanaUnavailable(RuntimeError):
    pass


class SolanaRpcError(RuntimeError):
    pass


@dataclass(frozen=True)
class PaymentVerification:
    state: str
    amount_lamports: int = 0
    source_address: str | None = None
    signature: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class RefundPreparation:
    signed_transaction: bytes
    transaction_signature: str


class _SyncRpcClient:
    """Small synchronous bridge for solana-py 0.40's AsyncClient-only API."""

    def __init__(self, async_client_class, rpc_url: str):
        self.async_client_class = async_client_class
        self.rpc_url = rpc_url

    def __getattr__(self, method_name):
        def call(*args, **kwargs):
            async def run():
                async with self.async_client_class(self.rpc_url) as client:
                    return await getattr(client, method_name)(*args, **kwargs)

            return asyncio.run(run())

        return call


class SolanaService:
    def __init__(self, rpc_url: str):
        self.rpc_url = rpc_url
        try:
            from solana.rpc.async_api import AsyncClient
            from solana.rpc.commitment import Finalized
            from solders.keypair import Keypair
            from solders.message import Message
            from solders.pubkey import Pubkey
            from solders.signature import Signature
            from solders.system_program import TransferParams, transfer
            from solders.transaction import Transaction
        except ImportError as error:
            self._imports_error = error
            return

        self.client = _SyncRpcClient(AsyncClient, rpc_url)
        self.finalized = Finalized
        self.Keypair = Keypair
        self.Message = Message
        self.Pubkey = Pubkey
        self.Signature = Signature
        self.TransferParams = TransferParams
        self.transfer = transfer
        self.Transaction = Transaction
        self._imports_error = None

    def _require_sdk(self):
        if self._imports_error is not None:
            raise SolanaUnavailable(
                "Install solana and solders to use Devnet RPC: pip install -r requirements.txt"
            ) from self._imports_error

    def validate_address(self, address: str) -> bool:
        self._require_sdk()
        try:
            self.Pubkey.from_string(address)
            return True
        except Exception:
            return False

    def validate_signature(self, signature: str) -> bool:
        self._require_sdk()
        try:
            self.Signature.from_string(signature)
            return True
        except Exception:
            return False

    def generate_wallet(self) -> tuple[str, bytes]:
        self._require_sdk()
        keypair = self.Keypair()
        try:
            private_bytes = bytes(keypair)
        except TypeError:
            private_bytes = keypair.to_bytes()
        return str(keypair.pubkey()), private_bytes

    @staticmethod
    def _json(value: Any) -> Any:
        if value is None or isinstance(value, (dict, list, str, int, float, bool)):
            return value
        if hasattr(value, "to_json"):
            try:
                return json.loads(value.to_json())
            except (TypeError, ValueError):
                pass
        if hasattr(value, "value"):
            return SolanaService._json(value.value)
        if hasattr(value, "__dict__"):
            return {key: SolanaService._json(item) for key, item in value.__dict__.items()}
        return value

    @staticmethod
    def _find_result(value: Any) -> Any:
        value = SolanaService._json(value)
        if isinstance(value, dict) and "result" in value:
            return value["result"]
        return value

    def verify_payment(
        self, signature: str, destination: str, expected_lamports: int
    ) -> PaymentVerification:
        self._require_sdk()
        if not self.validate_signature(signature):
            return PaymentVerification("invalid", signature=signature, reason="Invalid transaction signature")
        try:
            response = self.client.get_transaction(
                self.Signature.from_string(signature),
                encoding="jsonParsed",
                commitment=self.finalized,
                max_supported_transaction_version=1,
            )
            transaction = self._find_result(response)
        except Exception as error:
            raise SolanaRpcError("Devnet transaction lookup failed") from error

        if not transaction:
            return PaymentVerification("invalid", signature=signature, reason="Transaction not found")
        meta = transaction.get("meta") or {}
        if meta.get("err") is not None:
            return PaymentVerification("invalid", signature=signature, reason="Transaction failed on Solana")

        instructions = (
            transaction.get("transaction", {})
            .get("message", {})
            .get("instructions", [])
        )
        transfers = []
        for instruction in instructions:
            parsed = instruction.get("parsed") if isinstance(instruction, dict) else None
            if not parsed:
                continue
            if instruction.get("program") != "system":
                continue
            if parsed.get("type") == "transfer":
                info = parsed.get("info", {})
                transfers.append(info)

        matching = [item for item in transfers if item.get("destination") == destination]
        if len(transfers) != 1 or len(matching) != 1:
            return PaymentVerification(
                "needs_review",
                signature=signature,
                reason="Only one straightforward native SOL transfer is supported",
            )

        transfer_info = matching[0]
        try:
            amount = int(transfer_info["lamports"])
        except (KeyError, TypeError, ValueError):
            return PaymentVerification("needs_review", signature=signature, reason="Transfer amount was ambiguous")
        state = "exact" if amount == expected_lamports else "overpayment" if amount > expected_lamports else "underpayment"
        return PaymentVerification(
            state,
            amount_lamports=amount,
            source_address=transfer_info.get("source"),
            signature=signature,
        )

    def get_balance(self, address: str) -> int:
        self._require_sdk()
        try:
            response = self.client.get_balance(self.Pubkey.from_string(address), commitment=self.finalized)
            value = getattr(response, "value", None)
            if value is None and isinstance(response, dict):
                value = response.get("result", {}).get("value")
            return int(value or 0)
        except Exception as error:
            raise SolanaRpcError("Devnet balance lookup failed") from error

    def get_minimum_balance_for_rent_exemption(self, data_length: int = 0) -> int:
        """Return the current lamport reserve needed for a system account."""
        self._require_sdk()
        try:
            response = self.client.get_minimum_balance_for_rent_exemption(data_length)
            value = getattr(response, "value", None)
            if value is None and isinstance(response, dict):
                value = response.get("result")
            return int(value or 0)
        except Exception as error:
            raise SolanaRpcError("Devnet rent exemption lookup failed") from error

    def get_signatures_for_address(self, address: str, limit: int = 20) -> list[dict[str, Any]]:
        """Return recent signature metadata for a checkout address.

        This is deliberately read-only. It lets the merchant refresh payment
        records from the unique checkout address without asking the customer
        to submit a recovery claim.
        """
        self._require_sdk()
        try:
            response = self.client.get_signatures_for_address(
                self.Pubkey.from_string(address),
                limit=limit,
                commitment=self.finalized,
            )
            result = self._find_result(response)
            return result if isinstance(result, list) else []
        except Exception as error:
            raise SolanaRpcError("Devnet payment history lookup failed") from error

    def prepare_transfer(
        self,
        private_key: bytes,
        destination: str,
        amount_lamports: int,
    ) -> RefundPreparation:
        """Build one signed native SOL transfer without broadcasting it."""
        self._require_sdk()
        try:
            source = self.Keypair.from_bytes(private_key)
            latest = self.client.get_latest_blockhash(commitment=self.finalized).value.blockhash
            instruction = self.transfer(
                self.TransferParams(
                    from_pubkey=source.pubkey(),
                    to_pubkey=self.Pubkey.from_string(destination),
                    lamports=amount_lamports,
                )
            )
            message = self.Message([instruction], source.pubkey())
            transaction = self.Transaction([source], message, latest)
            signed_bytes = bytes(transaction)
            signature = str(transaction.signatures[0])
            return RefundPreparation(signed_bytes, signature)
        except Exception as error:
            raise SolanaRpcError("Could not sign native SOL transfer") from error

    def prepare_refund(
        self,
        encrypted_private_key: bytes,
        destination: str,
        amount_lamports: int,
    ) -> RefundPreparation:
        try:
            return self.prepare_transfer(encrypted_private_key, destination, amount_lamports)
        except SolanaRpcError as error:
            raise SolanaRpcError("Could not sign refund transaction") from error

    def broadcast_signed_transaction(self, signed_transaction: bytes) -> str:
        self._require_sdk()
        try:
            from solana.rpc.models import TxOpts
            from solana.rpc.commitment import Finalized

            response = self.client.send_raw_transaction(
                signed_transaction,
                opts=TxOpts(skip_preflight=False, preflight_commitment=Finalized),
            )
            value = getattr(response, "value", None)
            if value is None and isinstance(response, dict):
                value = response.get("result")
            if not value:
                raise SolanaRpcError("Devnet did not return a transaction signature")
            return str(value)
        except SolanaRpcError:
            raise
        except Exception as error:
            detail = str(error).strip()
            message = "Devnet transaction broadcast failed"
            if detail:
                message = f"{message}: {detail}"
            raise SolanaRpcError(message) from error

    def signature_status(self, signature: str) -> str:
        self._require_sdk()
        try:
            response = self.client.get_signature_statuses([self.Signature.from_string(signature)])
            result = self._find_result(response) or {}
            values = result.get("value", []) if isinstance(result, dict) else []
            status = values[0] if values else None
            if not status:
                return "unknown"
            if status.get("err") is not None:
                return "failed"
            return status.get("confirmationStatus", "unknown")
        except Exception as error:
            raise SolanaRpcError("Devnet transaction status lookup failed") from error
