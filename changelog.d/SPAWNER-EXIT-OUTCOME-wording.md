Clarify the spawner exit-outcome documentation (D-4573): denied-review exact
trailing verdicts are preserved from pending JSON text without a stop frame,
but text after an explicit non-stop finish is not promoted. This aligns the
wording with docs/SPAWNER.md and the existing verdict tests; no runtime behavior
changes.
