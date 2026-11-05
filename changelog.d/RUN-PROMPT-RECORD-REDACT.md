### Fixed

- Align Python and Rust credential redaction on word-start `sk-` and `xsk-`
  prefixes with any nonempty ASCII credential suffix, including short seeded
  secrets. Preserve embedded prefixes in ordinary words such as `risk-free`,
  `task-1`, and `desk-lamp`; `sk-learn` is intentionally redacted.
- Exercise both implementations against one synthetic JSON parity table.
  Other credential rules are unchanged.
