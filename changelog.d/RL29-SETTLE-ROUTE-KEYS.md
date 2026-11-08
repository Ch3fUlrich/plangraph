### Fixed

- Reconciliation settles Python-spawner orphan records without requiring synthetic
  leg/route keys. It derives leg bookkeeping keys using Python's model/harness
  rule; when route attribution is unavailable (Python does not write or compute a
  route key), it logs run/attempt and the missing attribution and records why the
  outcome row was skipped. This also settles leg-key-only records previously
  refused for lacking a route key; fully keyed outcomes retain their attribution.
  Non-object legs and wrong-typed keys remain invalid. Real Python records cannot
  produce attributed outcomes until a route identity contract exists (D-4691).
