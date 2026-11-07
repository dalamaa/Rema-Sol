# RemaSol — hackathon submission

## The problem

Small merchants accepting direct cryptocurrency payments eventually receive an accidental overpayment. A 0.05 SOL product may arrive with 0.50 SOL attached. The customer has to explain the mistake, provide a transaction ID, and wait. The merchant then verifies the payment, calculates the excess, finds the right destination, and tries not to refund it twice.

## The solution

RemaSol is an open-source, self-hosted Solana payment recovery tool. It gives every checkout a unique payment room and keeps the purchase flow focused on payment. Once a customer provides a private claim code, sender wallet, and original transaction signature, RemaSol verifies the finalized native SOL transfer directly on Devnet. The original sender is derived from that verified transfer; without suitable proof of sender-wallet control, an excess-payment case goes to manual review. If the payment is over the product price, the customer can request exactly the verified excess.

The core promise is simple: **your customers shouldn't need to contact support just because they sent too much SOL.**

## Why it matters

- Fewer customer support conversations.
- Less manual payment investigation.
- Faster overpayment resolution.
- Fewer duplicate-refund mistakes.
- Clear transaction records that can be inspected on Solana Explorer.
- Self-hosted merchant control rather than a percentage-based payment gateway.

Self-hosting does not mean zero operating costs. Merchants still pay network fees and may pay for RPC, hosting, and operations.

## Security decisions

- All financial values use integer lamports, never floating point.
- Every order gets a distinct payment address.
- Claim codes use cryptographically secure randomness and are stored only as salted PBKDF2 hashes.
- Private checkout keys are encrypted at rest with a Fernet key outside SQLite.
- Ordinary customers cannot submit or replace a refund destination; the verified transfer sender is the only derived destination.
- Funded demo orders explicitly bind the application-controlled buyer wallet before payment.
- The source address is read from the transfer instruction; RemaSol does not assume the fee payer is the customer.
- Unsupported or ambiguous transfer patterns are sent to manual review.
- Unique database constraints plus `BEGIN IMMEDIATE` reserve one refund per order.
- Signed transaction bytes and their expected signature are persisted before the first broadcast.
- An uncertain broadcast never causes RemaSol to construct a second transaction.
- CSRF protection, request limits, input validation, SQL parameters, and per-process rate limits are included.
- Merchant administration is session-protected and is disabled until a password hash is configured; customer checkout and recovery do not require merchant credentials.

The claim code is a bearer credential. Someone who steals it may access the claim, just as stolen account credentials create risk elsewhere. RemaSol does not claim to verify exchange-account ownership or guarantee fraud prevention.

## Technology

Python 3.11+, Flask, Jinja, SQLite, `solana-py`, `solders`, `cryptography`, HTML, CSS, lightweight JavaScript, and pytest. No custom Solana program is required for the MVP.

## What is implemented

The merchant console, unique checkout wallet, claim flow, Devnet RPC verification adapter, exact/under/overpayment states, durable refund state machine, idempotent refund path, Explorer receipts, standalone Test Wallet, dedicated admin treasury, sender-bound recovery form, rate limits, UI, documentation, and mocked test suite are implemented.

## Public demo and treasury boundary

The Test Wallet at `/wallet` uses a generated temporary sender wallet and can pay only active RemaSol checkout addresses. Visitors may test 0.0005–0.005 SOL per payment, including 0.001 SOL underpayment, 0.002 SOL exact payment, 0.004 SOL overpayment, and 0.005 SOL maximum overpayment; `/demo` redirects to `/wallet` for compatibility. The merchant treasury at `/merchant/treasury` is authenticated, capped, manually funded, and separate from customer checkout wallets. Wallet creation is not a treasury operation; only in-progress funded operations consume the atomic concurrency limit, while unresolved signed transfers retain their reservations. There is no cumulative visitor credit or arbitrary withdrawal flow.

The Test Wallet records the funding and payment signed transaction bytes before broadcast. If an RPC response is lost, retries reuse the saved signature and move to review when finalization cannot be established. It does not create checkout credentials; customers use the separate checkout and recovery pages when an excess payment needs review.

## What remains before mainnet production

Managed key custody, merchant identity/SSO, key rotation, distributed rate limiting, monitoring and reconciliation workers, treasury-liability accounting, manual-review tooling, deeper transaction parsing, formal threat modeling, audited transaction construction, exchange-specific guidance, and production operational controls.

RemaSol is a working Devnet MVP, not a production-ready custody system.
