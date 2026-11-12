- Preserve source expressions in Python and Rust prompt and output credential-assignment redaction,
  including interpolated strings, calls, operators and lowercase dotted attribute paths in review diffs;
  fail closed on bare lowercase credential values while keeping independent secret-pattern scrubbing.
- Cover bare credential values in the shared Python/Rust parity table and verify review prompt
  records and adapter payloads retain source expressions while masking a synthetic credential.
