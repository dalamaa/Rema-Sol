# RemaSol — hackathon submission

## The problem

Small merchants accepting direct cryptocurrency payments eventually receive an accidental overpayment. A 0.05 SOL product may arrive with 0.50 SOL attached. The customer has to explain the mistake, provide a transaction ID, and wait. The merchant then verifies the payment, calculates the excess, finds the right destination, and tries not to refund it twice.

## The solution

RemaSol is an open-source, self-hosted Solana payment recovery tool. It gives every checkout a unique payment room and captures the customer's refund destination before payment. Once a customer provides a private claim code and the original transaction signature, RemaSol verifies the finalized native SOL transfer directly on Devnet. If the payment is over the product price, the customer can request exactly the verified excess.

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
- Refund destinations are locked before payment.
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

The merchant console, unique checkout wallet, claim flow, Devnet RPC verification adapter, exact/under/overpayment states, durable refund state machine, idempotent refund path, Explorer receipts, rate limits, UI, documentation, and mocked test suite are implemented.

## What remains before mainnet production

Managed key custody, merchant identity/SSO, key rotation, distributed rate limiting, monitoring and reconciliation workers, treasury-liability accounting, manual-review tooling, deeper transaction parsing, formal threat modeling, audited transaction construction, exchange-specific guidance, and production operational controls.

RemaSol is a working Devnet MVP, not a production-ready custody system.
