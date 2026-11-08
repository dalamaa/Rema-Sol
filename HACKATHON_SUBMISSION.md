# RemaSol — MLH Solana Hackathon submission

> Keep the payment. Return the extra.

**Watch Demo:** [Official demo video](https://youtu.be/0r_kt_4iHrA)<br>
**Live application:** [remasol.ddnsfree.com](https://remasol.ddnsfree.com/)<br>
**Interactive demo:** [remasol.ddnsfree.com/demo](https://remasol.ddnsfree.com/demo)<br>
**GitHub:** [github.com/dalamaa/Rema-Sol](https://github.com/dalamaa/Rema-Sol)

## Project summary

RemaSol is a self-hosted Solana payment-recovery prototype for small merchants. When a customer sends more SOL than a product costs, RemaSol verifies the finalized native transfer, calculates the exact excess in lamports, and gives eligible cases a guided recovery path. Ambiguous payments or unverified refund destinations go to manual review instead of an unsafe automatic payout.

This is a Solana Devnet hackathon prototype, not mainnet-ready custody software.

## The problem and solution

A customer buys a **0.002 SOL** product but accidentally sends **0.005 SOL**. The merchant must identify the payment, confirm the destination and amount, calculate the **0.003 SOL** excess, determine where it can safely go, issue a refund, and avoid paying twice. That work creates support messages and manual investigation for an otherwise valid purchase.

RemaSol creates a unique checkout address and private Merchant Code for each order. It reads the actual native SOL transfer from Solana Devnet, records exact/underpaid/overpaid states, and uses a durable refund state machine for eligible recovery. A controlled Test Wallet binds its generated sender before payment, enabling the verified demo path. Ordinary public checkout can derive the transfer source but does not pretend that a source address proves customer or exchange-account ownership; uncertain cases remain in manual review.

## Merchant benefits

- Less manual calculation and payment investigation for eligible overpayments.
- A clearer customer path than opening a support ticket for every accidental excess.
- Durable payment and refund records with public Solana Explorer proof.
- Atomic one-refund-per-order reservation and saved signed transactions to reduce duplicate-payout risk.
- Self-hosted merchant control rather than a percentage-based gateway.

These are intended workflow benefits, not measured production results. Merchants still pay network, RPC, hosting, and operational costs.

## Why Solana matters

Solana is the payment, verification, and refund settlement layer. RemaSol uses real native SOL transfers, checks the source, destination, amount, signature, and finalization through Solana Devnet RPC, and exposes independent payment/refund proof in Solana Explorer. Low network fees help make small refunds practical, while the chain’s asynchronous finality means the application must persist candidates and reconcile uncertain outcomes rather than blindly retry.

The prototype uses native SOL transfers and does not require a custom Solana program.

## Implemented functionality

- Authenticated Merchant Dashboard with product creation, shareable checkout links, recent verified activity, totals, and explicit Devnet refresh.
- Unique payment wallet and private Merchant Code per checkout.
- Exact, underpayment, overpayment, expiry, duplicate-signature, ambiguous-transfer, and `needs_review` states.
- Integer lamport arithmetic and direct native SOL verification through Solana Devnet RPC.
- Standalone Test Wallet with temporary sender wallets, a separate manually funded demo treasury, active-checkout-only recipients, amount/daily/concurrency caps, and durable retry state.
- Guided Recovery requiring the Merchant Code, actual sender address, and payment transaction ID.
- Sender-bound refund destinations for the controlled demo; no claimant-supplied arbitrary refund wallet.
- Durable SQLite refund records, unique order/refund and transaction-signature constraints, atomic reservation, signed-transaction persistence before broadcast, reconciliation, and idempotent repeat requests.
- Manual review for uncertain broadcasts, insufficient balance, unsupported transfers, and unverified ownership/destinations.
- CSRF protection, password-hash merchant authentication, request validation, parameterized SQL, rate limits, encrypted application-controlled private keys, and mocked Solana RPC tests.

Underpayments are detected and recorded. A customer top-up or outstanding-balance payment flow is future work, not an implemented automatic feature.

## Technologies

Python 3.11+, Flask, Jinja, SQLite, `solana-py`, `solders`, `cryptography`/Fernet, Werkzeug password hashing, HTML/CSS, lightweight JavaScript, pytest, and Docker/Gunicorn deployment files.

## Verified Devnet evidence

The hosted application completed this real end-to-end path:

| Event | Amount |
| --- | ---: |
| Product price | **0.002 SOL** |
| Payment | **0.005 SOL** |
| Verified excess | **0.003 SOL** |
| Finalized refund | **0.003 SOL** |

- [0.005 SOL payment on Solana Explorer](https://explorer.solana.com/tx/2C1o5fTENTVKq5RQNbrN8FaHjA63mtrJCQh5aKqLbXrEJK8XMuknBT4cAQX1r6gn4xGk2PxQPuKTijiFoN4riref?cluster=devnet)
- [Finalized 0.003 SOL refund on Solana Explorer](https://explorer.solana.com/tx/aRmgM5nrseWm16BZqBP9jUgLcJWHgPs8NECinWpHfQdNCbrmJyDHG95BWbKBf1eFHCcEZ35xj7H2GcycsDnYDrS?cluster=devnet)

The documented signatures resolve through Solana Devnet RPC as successful finalized native SOL transfers. The automated suite is separate: `pytest -q` currently reports **68 passed** using a fake Solana service.

## Challenges and lessons

- **Finality is asynchronous:** a browser can lose a response while a transaction is still settling, so signed bytes, expected signatures, and operation states must be durable.
- **Refunds need idempotency:** atomic reservation and unique constraints are required so retries cannot create a second payout candidate.
- **Payment amount is not enough:** destination, source, transaction shape, order assignment, and sender/refund-destination evidence all affect eligibility.
- **Public demos need a treasury boundary:** the Test Wallet has a dedicated manually funded treasury, active-checkout-only recipients, capped amounts, daily/concurrent limits, and conservative unresolved-funds accounting.
- **Prototype custody has limits:** encrypted SQLite keys and a controlled login are useful demo safeguards, but they are not a production KMS, identity system, audit, or threat model.

## Future work

Before mainnet use, RemaSol would need managed key custody, key rotation, independent security review, formal threat modeling, stronger merchant identity and policy controls, distributed rate limiting, monitoring and reconciliation workers, manual-review tooling, explicit treasury/liability accounting, exchange-specific guidance, and audited transaction construction. A guided underpayment balance-payment flow is also future work.

## AI-assisted development acknowledgment

Development used AI-assisted coding tools, including Codex, with local source review, automated tests, mocked Solana RPC tests, and real read-only verification of the documented Devnet transactions. AI assistance does not change the project’s Devnet-only prototype status or replace security review.
