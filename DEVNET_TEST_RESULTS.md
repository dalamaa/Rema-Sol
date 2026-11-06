# Public Solana Devnet Test Results

All transactions below were finalized on `https://api.devnet.solana.com` using the existing customer wallet. No claim codes or private keys are stored in this report.

- Customer wallet: `DHarYozbuJF9GVxGCt991ZMK468N94NKzNBvYQcd82qq`
- Balance before tests: `2.500000000 SOL`
- Initial payment volume: `0.065000000 SOL` (`65,000,000` lamports)
- Balance after tests: `2.454980000 SOL`
- Refund records created: `2`

## Scenarios

| Scenario | Price | Payment | Result | Refund | Customer balance change | Payment | Refund |
|---|---:|---:|---|---:|---:|---|---|
| Overpayment 1 | 0.01 SOL | 0.02 SOL | Finalized overpayment; 0.01 SOL excess | 0.01 SOL | +0.01 SOL | [Explorer](https://explorer.solana.com/tx/rkb5zaKAvTz7cRsWBoAFzpWh2hEQTYD8qvG4fhqWxbk7NNWNDQpwLY3vEkf1BBpJrd4pV1ygzd41HcdDz7XBPee?cluster=devnet) | [Explorer](https://explorer.solana.com/tx/522MKjSQFGtu1bx2YqpyNErV17ctewyehghdaJgZbCxNg2DptyAYkaYNMfLxvZMcYtmAtCXzdpZHND5Pz7f6FxvB?cluster=devnet) |
| Exact payment | 0.01 SOL | 0.01 SOL | Finalized exact payment; no refund eligible | 0 SOL | — | [Explorer](https://explorer.solana.com/tx/2RgrVgUq2gNY6DdrVQS6MfitSXZHzStqXeEotWHL8n3pxYAbfrU1X4ZrPhswcdDjDQCGm7gsNWsMsuuz26JLzRdE?cluster=devnet) | — |
| Underpayment | 0.01 SOL | 0.005 SOL | Finalized underpayment; 0.005 SOL shortfall | 0 SOL | — | [Explorer](https://explorer.solana.com/tx/2vZzzw9VefM3yuo8TssqfTSgERNeLtp5Rxu6avbHminmgGZbHST8k8e6H5tCHLKcGKEKV9iDEjFyAuwDrVxNHgkW?cluster=devnet) | — |
| Overpayment 2 | 0.02 SOL | 0.03 SOL | Finalized overpayment; 0.01 SOL excess | 0.01 SOL | +0.01 SOL | [Explorer](https://explorer.solana.com/tx/63JvyCgQcgXvKoZ2berPu1ZGasvxanBuqrsovsmL1EVAH6zz4U7BN79d6W6MqgxjQENxu69K7CTMEJswaV6z18bf?cluster=devnet) | [Explorer](https://explorer.solana.com/tx/4vfnZArvNiFKBayux7KLbj9SE3XibFJkAXbySRgb5sFScqHr3HhvqqaF9XSfd2VU9PPCA9H7v3PL7iBxWL3spAHs?cluster=devnet) |

Both refund destinations matched the address locked at checkout. Refund balance increases were exactly `10,000,000` lamports each; refund fees were paid by the checkout wallets.

Exact customer balances around refunds:

- Overpayment 1: `2,434,980,000` → `2,444,980,000` lamports (`+10,000,000`).
- Overpayment 2: `2,444,980,000` → `2,454,980,000` lamports (`+10,000,000`).

Payment signatures:

- Overpayment 1: `rkb5zaKAvTz7cRsWBoAFzpWh2hEQTYD8qvG4fhqWxbk7NNWNDQpwLY3vEkf1BBpJrd4pV1ygzd41HcdDz7XBPee`
- Exact payment: `2RgrVgUq2gNY6DdrVQS6MfitSXZHzStqXeEotWHL8n3pxYAbfrU1X4ZrPhswcdDjDQCGm7gsNWsMsuuz26JLzRdE`
- Underpayment: `2vZzzw9VefM3yuo8TssqfTSgERNeLtp5Rxu6avbHminmgGZbHST8k8e6H5tCHLKcGKEKV9iDEjFyAuwDrVxNHgkW`
- Overpayment 2: `63JvyCgQcgXvKoZ2berPu1ZGasvxanBuqrsovsmL1EVAH6zz4U7BN79d6W6MqgxjQENxu69K7CTMEJswaV6z18bf`

Refund signatures:

- Overpayment 1: `522MKjSQFGtu1bx2YqpyNErV17ctewyehghdaJgZbCxNg2DptyAYkaYNMfLxvZMcYtmAtCXzdpZHND5Pz7f6FxvB`
- Overpayment 2: `4vfnZArvNiFKBayux7KLbj9SE3XibFJkAXbySRgb5sFScqHr3HhvqqaF9XSfd2VU9PPCA9H7v3PL7iBxWL3spAHs`

## Duplicate and invalid-request checks

- Repeating the first refund request returned the same finalized refund signature and changed the customer balance by `0` lamports.
- Four simultaneous requests for the second overpayment returned one refund ID and one refund signature. Only one `10,000,000`-lamport payout occurred.
- Incorrect claim code: rejected.
- Valid claim for the wrong order: rejected.
- Reused payment signature on another order: returned `needs_review`; no payout occurred.
- Invalid-request balance: unchanged.
- Invalid-request refund-row count: unchanged.

No mainnet or local-validator transactions are included here.
