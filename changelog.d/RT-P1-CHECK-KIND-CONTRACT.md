Normalize legacy empty claim check kinds to `change` in Python, matching Rust.
Keep unknown recorded Rust claim kinds readable while rejecting execution as a
contract defect. Cover metadata parity, legacy decoding, and raw-empty red-first
execution without relaxing the unknown-kind validation contract.
Bind backend-specific metadata query placeholders, compare SQLite results even
without local Postgres, and test recorded unknown-kind execution refusal.
Remove a redundant backend assertion and extend check-kind coverage to malformed
stored cards, case/whitespace variants, and non-string recorded JSON values.
