# WORK-MODE-S1-CONFIG

Add the shared, extensible WorkMode setting (`auto`, `piecewise`, `batch`) and
Rust config loader with repository > user > default batch precedence. Invalid
selected configuration returns a clear error. Reuse the existing spawner XDG
config-root helper, document the schema and example, and pin this repo to batch.
Auto selection and UI follow in later slices (D-5952, rule 3.41).
