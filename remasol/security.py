from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from datetime import datetime, timezone

from cryptography.fernet import Fernet, InvalidToken


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat()


def generate_claim_code() -> str:
    # 24 random bytes gives the bearer credential more than 128 bits of entropy.
    return base64.urlsafe_b64encode(secrets.token_bytes(24)).decode().rstrip("=")


def hash_claim_code(code: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", code.encode("utf-8"), salt, 180_000, dklen=32
    )
    return f"{salt.hex()}${digest.hex()}"


def verify_claim_code(code: str, stored: str) -> bool:
    try:
        salt_hex, digest_hex = stored.split("$", 1)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
    except (TypeError, ValueError):
        return False
    actual = hashlib.pbkdf2_hmac(
        "sha256", code.encode("utf-8"), salt, 180_000, dklen=32
    )
    return hmac.compare_digest(actual, expected)


def wallet_cipher(key: str) -> Fernet:
    if not key:
        raise RuntimeError(
            "REMA_SOL_WALLET_KEY is required to encrypt checkout wallet keys. "
            "Generate one with: python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'"
        )
    try:
        return Fernet(key.encode() if isinstance(key, str) else key)
    except (ValueError, TypeError) as error:
        raise RuntimeError("REMA_SOL_WALLET_KEY must be a valid Fernet key") from error


def encrypt_private_key(private_key: bytes, key: str) -> str:
    return wallet_cipher(key).encrypt(private_key).decode()


def decrypt_private_key(value: str, key: str) -> bytes:
    try:
        return wallet_cipher(key).decrypt(value.encode())
    except InvalidToken as error:
        raise RuntimeError("Stored checkout wallet key could not be decrypted") from error
