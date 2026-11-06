# RemaSol demo script

This is a Devnet-only demo. Use free Devnet SOL from the official Solana faucet; never purchase real SOL for this flow.

## Setup

1. Start RemaSol with `python app.py` (default: `http://127.0.0.1:5007`; set `PORT` to change it).
2. Open `/merchant` and sign in with the demo merchant credentials configured in `.env`.
3. Create a product named `Field Notes pack` with a price of `0.05` SOL.
4. Open the generated checkout link in a second browser tab.

## Customer payment room

1. Enter a Devnet wallet you control as the refund destination.
2. Create the checkout and point out the unique payment address.
3. Point out the private claim code. Explain that it is shown once, stored only as a secure hash, and is a bearer credential.
4. Send `0.50` Devnet SOL to the payment address.
5. Wait for finalization and paste the transaction signature into the payment room.

## Show the recovery

1. Show that RemaSol reads the actual native SOL transfer and reports:
   - product price: `0.05 SOL`
   - received: `0.50 SOL`
   - refundable excess: `0.45 SOL`
2. Open `/recover`.
3. Enter the payment address, claim code, and original payment signature.
4. Show that the destination is locked and the refund button is enabled only after verification.
5. Request the refund.
6. Show the exact refund signature and open the Devnet Solana Explorer link.
7. Submit the same request again. Explain that it returns the existing refund state and cannot create a second payout.

The intended live proof is one payment signature and one refund signature. Copy both into the presentation notes together with their Devnet Explorer URLs. If the SDK, faucet, or RPC is unavailable, use the mocked test suite and say the live path is unverified.

## Safety narration

- A transaction signature is public; the claim code is private.
- The claim code proves access to this checkout, not ownership of an exchange account.
- Exchange hot wallets are not treated as the customer's refund destination.
- RemaSol is not claiming zero costs: Solana fees, RPC, and hosting still apply.
- If RPC times out after broadcast, the signed bytes and signature remain durable and the refund moves to manual review instead of risking a second payout.

## If live Devnet is unavailable

Do not pretend it worked. Run `pytest -q`, show the mocked verification and concurrency tests, and explain that the live faucet/RPC path remains unverified in this session.
