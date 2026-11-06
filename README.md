# RemaSol

> Keep the payment. Return the extra.

RemaSol is a self-hosted Solana payment recovery MVP for small merchants. When a customer sends more SOL than a digital product costs, RemaSol verifies the finalized Devnet transfer, calculates the excess in lamports, and gives the customer one secure path to request a refund without opening a support ticket.

This project is intentionally a Devnet prototype. It is not production custody software and must not be used with mainnet funds.

## What is implemented

- Flask + Jinja merchant console with product creation and shareable checkout links.
- A unique Solana payment wallet for every order.
- A private 192-bit claim code generated with `secrets`; only a salted PBKDF2 hash is stored.
- Refund destination captured before payment and never editable afterwards.
- Direct native SOL transfer verification through Solana Devnet RPC.
- Exact payment, underpayment, overpayment, ambiguous transfer, duplicate signature, and expired room states.
- Integer lamport calculations throughout the business logic.
- Durable SQLite refund records with unique order/refund and transaction-signature constraints.
- Atomic `BEGIN IMMEDIATE` refund reservation, signed transaction persistence before broadcast, and idempotent repeat requests.
- Uncertain broadcast outcomes become `needs_review`; RemaSol never constructs a second payout while the first signed transaction is unresolved.
- Per-process rate limits for checkout, claim, verification, and refund requests plus an outstanding-checkout cap.
- Session-protected merchant administration with a Werkzeug password hash; customer checkout and recovery remain public.
- CSRF-protected forms, bounded request size, parameterized SQL, validated addresses/signatures, and safe external Explorer links.
- Automated tests with a mocked Solana RPC service.

## Run locally

Python 3.11+ is recommended.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# paste that value into REMA_SOL_WALLET_KEY in .env
python -c "import secrets; print(secrets.token_urlsafe(32))"
# paste that value into REMA_SOL_SECRET_KEY in .env
python -c "from werkzeug.security import generate_password_hash; print(generate_password_hash('choose-a-demo-password'))"
# paste that value into REMA_SOL_MERCHANT_PASSWORD_HASH in .env
python app.py
```

Open <http://127.0.0.1:5007>. The local port is configurable with `PORT`; `python app.py` reads it from `.env`. The merchant console is at `/merchant` and customer recovery is at `/recover`. The console redirects to `/merchant/login` until the configured merchant username and password hash are supplied.

## Test

```bash
pytest -q
```

The tests use a fake Solana service. They intentionally do not claim that a live Devnet payment succeeded.

## Architecture

```text
Browser
  -> Flask routes + CSRF + rate limits
  -> PaymentEngine
       -> SQLite (products, orders, refunds)
       -> SolanaService (Devnet RPC, wallet generation, verification, signing)
```

Checkout private keys are encrypted with Fernet before they are written to SQLite. The Fernet key is environment configuration, not a database value. This is suitable for a local hackathon demo; production would need a managed KMS/HSM, operational key rotation, stronger observability, authenticated merchant access, and a carefully reviewed custody model.

The refund flow is deliberately durable:

1. Verify the claim, order, transaction, amount, and locked destination.
2. Check the checkout wallet balance.
3. Sign the exact refund transaction.
4. Persist the signed bytes and expected signature inside an atomic refund reservation.
5. Broadcast those same bytes.
6. Move to `submitted`, `finalized`, or `needs_review`.

If a broadcast response is lost, the signed transaction remains the only payout candidate. A repeat request reads the existing refund record; it does not create a new transaction.

## Exchange payments and limitations

An exchange withdrawal may come from an exchange-controlled hot wallet. RemaSol reads the actual source account from the native transfer, but it does not claim that source belongs to the customer and does not automatically refund to it. The registered refund destination and private claim code associate the payment with the checkout. Some exchanges require special deposit procedures, so a finalized Solana transaction does not guarantee that an exchange account credits it.

Before mainnet use, add a production key-management system, a durable distributed limiter, monitoring and reconciliation workers, explicit treasury/liability accounting, manual review tooling, policy controls, a formal threat model, and an audited transaction-building path. The MVP merchant login is suitable for a controlled demo, not a complete production identity system. Do not call this MVP production-ready.

## VPS deployment

The repository includes a Gunicorn systemd unit, Nginx HTTPS reverse-proxy configuration, and a deployment checklist in [`deploy/README.md`](deploy/README.md). The service binds only to `127.0.0.1`, stores SQLite under `/var/lib/remasol`, loads secrets from `/etc/remasol/remasol.env`, enables secure cookies, and trusts forwarded client identity only when `REMA_SOL_TRUST_PROXY=1` is set behind Nginx.

## Files

- `app.py` — Flask entrypoint.
- `remasol/services.py` — payment, verification, idempotent refund, and lamport business logic.
- `remasol/solana_service.py` — Solana Devnet adapter.
- `remasol/db.py` — SQLite schema and transactions.
- `remasol/templates/` and `remasol/static/` — responsive merchant/customer UI.
- `tests/` — mocked RPC and concurrency/security tests.
- `SECURITY_TEST_RESULTS.md` — controlled abuse-test evidence and remaining limitations.
- `DEMO_SCRIPT.md` — hackathon demonstration sequence.
- `HACKATHON_SUBMISSION.md` — submission-ready story and boundaries.
- `deploy/` — VPS deployment configuration and HTTPS checklist.
