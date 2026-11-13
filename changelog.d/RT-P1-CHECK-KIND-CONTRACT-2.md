Restore a separately named Rust raw-empty check-kind red-first regression and
fix worker-claim test import ordering. Preserve the merged contract: missing or
empty kinds mean `change`, unknown recorded strings remain readable, and unknown
kinds are rejected when building or executing claims. Backend parity continues
through the store adapter's SQLite-to-Postgres placeholder translation.
