# RemaSol Security and UX Test Results

Date: 2026-11-06

This pass used an isolated temporary SQLite database and a mocked Solana service for concurrency, failure-injection, and abuse-volume tests. It did not create or broadcast additional Devnet payments. Public Devnet evidence remains in [`DEVNET_TEST_RESULTS.md`](DEVNET_TEST_RESULTS.md); those links are real finalized Devnet transactions and are not represented by the mocked tests below.

## Verification summary

| Check | Result |
|---|---|
| Automated suite | **21 passed** with `pytest -q` |
| Python compilation | Passed with `python -m compileall -q app.py remasol tests` |
| Port availability | 5007 was free before startup |
| Local entrypoint | `python app.py` served `127.0.0.1:5007` and honored `PORT` |
| HTTP smoke flow | Passed against the running local server |
| Public network | Devnet-only configuration preserved; no mainnet endpoint or deployment added |

## Controlled security tests

- **Concurrent refund requests:** the existing 32-request simultaneous refund test passed. SQLite reservation and the refund state machine produced one refund record, one durable transaction signature, and one broadcast; the mocked payout count was one.
- **Duplicate refund:** a repeated request returned the existing finalized refund record and did not broadcast again.
- **Uncertain broadcast:** a simulated lost broadcast response persisted the signed transaction and signature, moved the refund to `needs_review`, and did not build or broadcast a second transaction.
- **Restart during uncertainty:** a new engine instance read the durable unresolved refund and still did not broadcast a second transaction.
- **Balance/RPC safety:** insufficient checkout balance and simulated RPC failures produced review-only outcomes without a payout.
- **Invalid claims and payment identity:** incorrect claim codes, wrong-order claims, copied transaction IDs, invalid/fake signatures, wrong destinations, ambiguous transfers, and unfinalized/uncertain states were rejected or sent to review without an additional payout.
- **Login abuse:** eight invalid login attempts were rejected; the ninth was rate-limited with HTTP 429.
- **Claim abuse:** repeated recovery checks hit the configured rate limit; no payment state changed.
- **CSRF:** missing CSRF tokens on recovery and authenticated merchant POSTs returned a themed HTTP 400 response.
- **Forwarded-header spoofing:** changing `X-Forwarded-For` did not create a new limiter identity when trusted-proxy mode was disabled. `ProxyFix` remains opt-in through `REMA_SOL_TRUST_PROXY=1`.
- **Checkout spam:** the isolated unpaid-checkout cap stopped creation after the configured outstanding-room limit.
- **Authorization:** unauthenticated merchant access redirects to `/merchant/login`; private checkout and receipt routes reject sessions that do not own the access token.

## Live local HTTP routes exercised

The running local server was exercised at:

- Home: `http://127.0.0.1:5007/`
- Merchant login: `http://127.0.0.1:5007/merchant/login`
- Merchant console: `http://127.0.0.1:5007/merchant`
- Recovery: `http://127.0.0.1:5007/recover`
- Temporary smoke-test product: `http://127.0.0.1:5007/p/http-inspection-product-2`
- Temporary smoke-test checkout: `http://127.0.0.1:5007/checkout/19fc7af7-a5c8-4530-b470-04feb5547747`

The smoke-test checkout used an isolated database under `/private/tmp`; it was not a public Devnet payment and its claim code was not recorded here.

## Changes made during this pass

- Added a configurable local `PORT` setting with a default of `5007`; the production Gunicorn/Nginx configuration was left unchanged.
- Added safe, themed 400/403/429 responses for malformed, unauthorized, and throttled requests.
- Added submit-state feedback and duplicate-click suppression to forms.
- Kept the recovery claim code masked in the recovery form and added live status semantics to result panels.
- Restored reachable navigation links at mobile widths.
- Added explicit regression tests for CSRF, login throttling, forwarded-header spoofing, checkout spam, and the durable one-signature boundary.

## Remaining limitations

- The limiter is per process. A multi-worker or multi-instance deployment still needs a shared limiter and centralized monitoring.
- Uncertain Solana outcomes intentionally stop at `needs_review`; an operator/reconciliation workflow is still required before mainnet use.
- Merchant authentication is deliberately a small controlled-demo login, not an enterprise identity system.
- Visual QA was performed from the templates and responsive CSS breakpoints in this environment; no browser automation package was installed for screenshot capture.
- The app remains a Devnet prototype. No deployment was performed and no mainnet funds are supported.
