# RemaSol demo script

This is a Devnet-only demo. Use free Devnet SOL from the official Solana faucet; never purchase real SOL for this flow.

## Setup

1. Start RemaSol with `python app.py` (default: `http://127.0.0.1:5007`; set `PORT` to change it).
2. Open `/merchant` and sign in with the demo merchant credentials configured in `.env`.
3. Open `/merchant/treasury`, copy the dedicated treasury address, and manually add a small amount of free Devnet SOL. Do not use mainnet funds; RemaSol does not fund this wallet automatically.
4. Open `/wallet` in a fresh browser session. The Test Wallet can send to the unique payment address from any active checkout.

## Test Wallet

1. Click `New Test` and point out the owner-scoped `/wallet/test/<session_id>` URL, temporary sender address, and `Maximum Test Payment: 0.02 SOL`.
2. Paste the checkout's unique payment address into Test Wallet and point out the capped amount presets.
3. Choose `0.005` SOL and click `Send Test SOL`. RemaSol funds the sender wallet from the dedicated treasury, then sends the selected amount to the checkout address.
4. Wait for both transfers to finalize. Show the payment transaction ID and Devnet Explorer link.
5. Click `New Test` again to show that a completed test does not block a fresh sender. Prior records remain durable internally for treasury reconciliation.

## Show the recovery

1. Show that RemaSol reads the actual native SOL transfer and reports:
   - product price: `0.002 SOL`
   - received: `0.005 SOL`
   - refundable excess: `0.003 SOL`
2. Open `/recover`.
3. Enter the Merchant Code, the temporary buyer sender address, and the payment transaction ID.
4. Show that the buyer address is locked as the refund destination and the refund button is enabled only after sender-bound verification.
5. Request the refund.
6. Show the exact refund signature and open the Devnet Solana Explorer link.
7. Submit the same request again. Explain that it returns the existing refund state and cannot create a second payout.

The intended live proof is one payment signature and one refund signature. Copy both into the presentation notes together with their Devnet Explorer URLs. If the SDK, faucet, or RPC is unavailable, use the mocked test suite and say the live path is unverified.

## Safety narration

- A transaction signature is public; the claim code is private.
- The claim code proves access to this checkout, not ownership of an exchange account.
- Exchange hot wallets are not treated as the customer's refund destination.
- Each `New Test` gets an independent sender, while each payment remains subject to the `0.02` SOL cap, daily-budget, concurrency, and unresolved-funds protections.
- The treasury has no arbitrary withdrawal control and cannot fund an existing customer wallet.
- RemaSol is not claiming zero costs: Solana fees, RPC, and hosting still apply.
- If RPC times out after broadcast, the signed bytes and signature remain durable and the refund moves to manual review instead of risking a second payout.

## If live Devnet is unavailable

Do not pretend it worked. Run `pytest -q`, show the mocked verification and concurrency tests, and explain that the live faucet/RPC path remains unverified in this session.
