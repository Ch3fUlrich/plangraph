### Fixed

- Reconciliation settles Python-spawner orphan records without requiring synthetic
  leg/route keys. It derives leg bookkeeping keys using Python's model/harness
  rule; when route attribution is unavailable (Python does not write or compute a
  route key), it logs and records why the outcome row was skipped. Existing keyed
  outcomes and all other settlement validation remain unchanged (D-4691).
