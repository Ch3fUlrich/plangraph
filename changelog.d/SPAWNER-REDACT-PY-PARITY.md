### Security

- Match Python prompt/source redaction to Rust's prefixed-token catalog, JWTs,
  and bounded wrapped-key joins; assert catalog and shared-fixture parity.
- Apply long-hex and mixed base64-blob rules only to child output, live logs and
  diagnostic tails, preserving prompt hashes and structured record identifiers.
- Fail closed on mid-line source-span boundaries and scan unterminated PEM
  headers linearly; cover final prompt records, output tails and notifications.
