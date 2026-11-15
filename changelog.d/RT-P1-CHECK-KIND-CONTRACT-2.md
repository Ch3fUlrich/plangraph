Exercise an explicit raw JSON `"check_kind": ""` through Rust deserialization,
normalization, and both red-first outcomes; retain the constructed-empty and
missing-field tests and worker-claim import ordering. Preserve the merged
contract: missing or empty kinds mean `change`, unknown recorded strings remain
readable, and unknown kinds are rejected when building or executing claims.
Backend parity continues through the store adapter's SQLite-to-Postgres
placeholder translation. No production change is needed.
