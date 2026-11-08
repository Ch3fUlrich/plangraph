SSH overflow receipts now require a unique per-call nonce sentinel pair and
ignore shell/env noise outside it. Missing or repeated pairs fail closed with
`receipt_unbracketed` without retrying acknowledged spawns. Receipt output uses
the existing credential masking helper, with hermetic boundary and masking tests.
