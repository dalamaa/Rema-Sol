# RemaSol

> Keep the payment. Return the extra.

[![Watch Demo](https://img.shields.io/badge/Watch%20Demo-YouTube-red?logo=youtube)](https://youtu.be/0r_kt_4iHrA) · [Live application](https://remasol.ddnsfree.com/) · [Interactive demo](https://remasol.ddnsfree.com/demo) · [Source code](https://github.com/dalamaa/Rema-Sol)

RemaSol is a self-hosted Solana payment-recovery prototype for small merchants. It addresses the awkward exception after a customer sends more SOL than a product costs: verify what actually settled, calculate the excess, and give an eligible customer a clear recovery path. Uncertain cases remain visible for manual review instead of becoming an unsafe automatic payout.

This repository documents an MLH Solana Hackathon prototype running on Solana Devnet. It is not production custody software and must not be used with mainnet funds.

## The merchant problem

Suppose a digital product costs **0.002 SOL**, but the customer accidentally sends **0.005 SOL**. The merchant should keep the agreed price and return the **0.003 SOL** excess.

Without a recovery workflow, the merchant has to match the transfer to an order, verify the amount and destination, calculate the difference, determine where a refund can safely go, send it, record the outcome, and answer the customer. A small payment mistake becomes support work and introduces the risk of paying twice.

RemaSol focuses on that post-payment exception. It does not claim to eliminate merchant operations: ambiguous payments, unverified destinations, uncertain network outcomes, and exchange-controlled source addresses can still require manual review.

## What RemaSol does

- **Merchant Dashboard:** authenticated product creation, shareable checkout links, recent locally verified payment and refund records, all-time totals, and an explicit Devnet refresh action.
- **Private checkout:** each order receives a unique Solana payment address and a private Merchant Code. A normal checkout does not ask the customer to type an arbitrary refund wallet.
- **Payment verification:** RemaSol reads the finalized transaction from Solana Devnet and checks the signature, native SOL transfer, receiving address, source address, amount, and order assignment.
- **Exact amount states:** payments are recorded as exact, underpaid, overpaid, expired, ambiguous, or otherwise needing review. All business amounts use integer lamports.
- **Test Wallet:** a separate, merchant-controlled Devnet treasury can create a temporary sender and make capped payments only to active RemaSol checkout addresses. Each test operation is durable and owner-scoped.
- **Guided Recovery:** customers can submit the Merchant Code, the actual sender address, and the payment transaction ID at `/recover`. The refund destination is derived from verified payment data or an internally bound Test Wallet sender; it is not replaced by a claimant-supplied destination.
- **Durable refunds:** one refund record is reserved per order. Signed transaction bytes and the expected signature are persisted before broadcast, so a repeated request or lost RPC response does not construct a second payout.
- **Manual review:** unsupported transfer patterns, missing ownership evidence, insufficient balance, failed/uncertain broadcasts, and other unsafe conditions become `needs_review` rather than triggering another transfer.

### Underpayments are identified, not topped up

RemaSol already detects and records an underpaid order and keeps it in a no-refund state. A guided process for paying the outstanding balance, combining multiple transfers, or automatically following up is future work. Automatic top-ups are not an implemented feature.

## The customer and merchant journey

1. Open the [interactive Demo](https://remasol.ddnsfree.com/demo), which explains the flow and links to the demo product, Test Wallet, Recovery, and merchant view.
2. On the demo product, create a checkout. RemaSol creates a unique payment address and displays the private Merchant Code; save both.
3. Use [Test Wallet](https://remasol.ddnsfree.com/wallet) to create a temporary sender, enter the checkout address, and choose a bounded Devnet amount. The built-in presets cover 0.001 SOL underpayment, 0.002 SOL exact payment, 0.004 SOL overpayment, and 0.005 SOL maximum overpayment.
4. RemaSol funds the temporary sender from the dedicated demo treasury and sends the selected amount to the checkout address. Funding and payment operations are capped, rate-limited, and persisted before broadcast.
5. RemaSol verifies the finalized native SOL transfer and reports the exact price, received amount, and payment state.
6. For the successful Test Wallet path, open [Recovery](https://remasol.ddnsfree.com/recover), enter the Merchant Code, sender address, and payment signature, then request the eligible excess.
7. The receipt shows the refund state and available Solana Explorer proof. The [Merchant Dashboard](https://remasol.ddnsfree.com/merchant/login) is authenticated; its login page includes a sanitized preview, while the real console shows database-backed records.

The public `/demo` route is a guide page. `/wallet` is the standalone Test Wallet entry point, and individual wallet tests use explicit owner-scoped `/wallet/test/<session_id>` URLs.

## Verified Solana Devnet example

This is a real end-to-end hosted Devnet example, not only a mocked test:

| Event | Amount |
| --- | ---: |
| Product price | **0.002 SOL** |
| Customer payment | **0.005 SOL** |
| Verified excess | **0.003 SOL** |
| Finalized refund | **0.003 SOL** |

- [View the 0.005 SOL payment in Solana Explorer](https://explorer.solana.com/tx/2C1o5fTENTVKq5RQNbrN8FaHjA63mtrJCQh5aKqLbXrEJK8XMuknBT4cAQX1r6gn4xGk2PxQPuKTijiFoN4riref?cluster=devnet)
- [View the finalized 0.003 SOL refund in Solana Explorer](https://explorer.solana.com/tx/aRmgM5nrseWm16BZqBP9jUgLcJWHgPs8NECinWpHfQdNCbrmJyDHG95BWbKBf1eFHCcEZ35xj7H2GcycsDnYDrS?cluster=devnet)

The payment is a successful 5,000,000-lamport native SOL transfer to the unique checkout address. The refund is a successful 3,000,000-lamport native SOL transfer back to the sender used by the controlled Test Wallet. Both signatures resolve on Solana Devnet with finalized transaction data.

## Why Solana is integral

Solana is the payment, verification, and refund settlement layer here; it is not an extra label on a conventional checkout.

- RemaSol handles real native SOL transfers rather than simulated balances.
- Solana RPC supplies the transaction signature, source, destination, amount, and finalization state used in payment verification. The customer does not choose the refundable amount from a form field.
- Small refunds are economically plausible when network fees are low, although merchants still have RPC, hosting, operations, and transaction-fee costs.
- Payment and refund records have independent, public Explorer proof.
- Fast settlement does not mean every browser response is immediate. RemaSol stores signed candidates and durable state so a delayed or lost response becomes reconciliation or manual review, not a blind second broadcast.

The prototype uses native SOL transfers and does not require a custom Solana program.

## Safeguards and boundaries

- **Amount integrity:** prices, payments, excesses, limits, and fee buffers are calculated as integer lamports.
- **Transfer integrity:** only a single straightforward native System Program transfer to the active checkout address is accepted for automatic classification. Ambiguous instruction sets, wrong destinations, invalid signatures, failed transactions, and reused signatures are rejected or sent to review.
- **Destination integrity:** ordinary public checkout derives the candidate refund destination from the verified transfer source, but it does not claim that a source address proves control of a customer or exchange account. Without a trusted ownership path, automatic refund eligibility remains conservative and the case goes to manual review. The controlled Test Wallet binds its generated sender to the order before payment, which is why the verified demo can use the guided refund path.
- **Claim protection:** Merchant Codes are generated with `secrets` and stored as salted PBKDF2-HMAC hashes. The code is a bearer credential and should be kept private.
- **Duplicate-payout protection:** the database enforces one refund per order and unique transaction signatures. `BEGIN IMMEDIATE` reserves the refund atomically; repeated requests reconcile the existing record.
- **Uncertain broadcasts:** the signed transaction and expected signature are saved before the first broadcast. If the response is lost, RemaSol does not construct another transaction; it moves the operation to `needs_review` or reconciles the saved signature.
- **Key protection:** application-controlled checkout and demo wallet private keys are encrypted with Fernet before being stored in SQLite. The Fernet key remains environment configuration.
- **Web protections:** merchant routes use password-hash authentication and sessions; forms use CSRF protection; inputs are bounded and validated; SQL is parameterized; rate limits are enabled per process.
- **Demo treasury limits:** Test Wallet payments are limited to active RemaSol checkout addresses, a configured per-payment maximum, a daily budget, and concurrent-operation reservations. The treasury is manually funded with Devnet SOL and has no arbitrary public withdrawal endpoint.

These are prototype safeguards, not a security audit or a production custody model. Do not call this project mainnet-ready.

## Architecture

```text
Browser
  -> Flask routes, sessions, CSRF, request limits
  -> PaymentEngine
       -> SQLite: products, orders, wallet operations, refunds
       -> SolanaService: Devnet RPC, verification, signing, status
  -> DemoTreasuryService: capped Test Wallet funding and payments
```

The refund path is deliberately durable:

1. Verify the Merchant Code, order, signature, amount, destination, and eligibility.
2. Check the checkout balance and fee buffer.
3. Sign the exact native SOL refund transaction.
4. Persist the signed bytes and expected signature inside an atomic reservation.
5. Broadcast those same bytes.
6. Reconcile to `submitted`, `finalized`, or `needs_review`.

## Technology

Python 3.11+, Flask, Jinja, SQLite, `solana-py`, `solders`, `cryptography`/Fernet, Werkzeug password hashing, HTML/CSS, lightweight JavaScript, pytest, and Docker/Gunicorn deployment files. No custom Solana program is required for the MVP.

## Run locally

Requirements: Python 3.11 or newer, a Solana Devnet RPC endpoint, and local environment configuration. Use test SOL only.

```bash
git clone https://github.com/dalamaa/Rema-Sol.git
cd Rema-Sol
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Generate values for the matching local `.env` variables:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
python -c "import secrets; print(secrets.token_urlsafe(32))"
python -c "from werkzeug.security import generate_password_hash; print(generate_password_hash('choose-a-local-demo-password'))"
```

Place those values in `REMA_SOL_WALLET_KEY`, `REMA_SOL_SECRET_KEY`, and `REMA_SOL_MERCHANT_PASSWORD_HASH`. Set a local merchant username in `REMA_SOL_MERCHANT_USERNAME`. Keep `.env`, wallet keys, Merchant Codes, and credentials out of Git and logs. The supplied `.env.example` defaults to Solana Devnet and port 5007.

Start the application:

```bash
python app.py
```

Open <http://127.0.0.1:5007>. Useful local routes are `/demo`, `/wallet`, `/recover`, `/merchant/login`, and the seeded demo product at `/p/the-digital-creator-s-guide`. The merchant console requires the configured local credentials. The demo treasury creates its dedicated wallet locally but does not fund itself; log in to `/merchant/treasury`, copy the public address, and fund it manually with Devnet SOL.

For a tracked local process, after installing the requirements you can also use:

```bash
./start.sh
./stop.sh
```

## Tests and verification

Run the automated suite:

```bash
pytest -q
```

At this documentation pass, the result is **68 passed**. The tests use a fake Solana service for repeatable verification, concurrency, security, and failure-injection cases; they do not claim that a live Devnet transaction succeeded. Python compilation also passes with:

```bash
python -m compileall -q app.py remasol tests
```

The two Explorer links above are the separate evidence for the real hosted Devnet payment and refund.

## Limitations and future work

RemaSol is intentionally a Devnet hackathon prototype. Before handling real mainnet funds, it would need at least:

- managed key custody/KMS or HSM, key rotation, and an independently audited transaction-building path;
- a formal threat model, stronger merchant identity/SSO, policy controls, monitoring, alerting, and centralized audit logs;
- a durable distributed rate limiter and background reconciliation workers;
- operator tooling for manual review, failed/uncertain payments, exchange-specific cases, and treasury/liability accounting;
- clearer production custody and refund policies, operational runbooks, and independent security review;
- a guided underpayment balance-payment flow, if that product direction is retained.

Uncertain Solana outcomes intentionally stop at `needs_review`. The current merchant login is suitable for a controlled demo, not a complete identity system. Self-hosting also does not mean zero operating costs.

## AI-assisted development

Development used AI-assisted coding tools, including Codex, alongside local source review, automated tests, mocked Solana RPC tests, and real read-only verification of the documented Devnet transactions. The acknowledgment does not change the project’s prototype status or substitute for security review.

## Repository map

- `app.py` — Flask entry point.
- `remasol/services.py` — checkout verification, lamport calculations, durable refund logic.
- `remasol/demo.py` — capped Test Wallet and demo treasury operations.
- `remasol/solana_service.py` — Solana Devnet RPC, native transfer verification, signing, and status.
- `remasol/db.py` — SQLite schema and migrations.
- `remasol/templates/` and `remasol/static/` — merchant/customer UI.
- `tests/` — automated behavior, safety, concurrency, and frontend regression tests.
- `deploy/` — Docker/Gunicorn/systemd/Nginx deployment notes for the hosted prototype.

**RemaSol. Keep the payment. Return the extra.**
