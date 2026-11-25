# Operator-turn fixtures select Rust CI

`rust/plangraph-cli/tests/operator_turns.rs` reads `tests/fixtures/operator_turns/**` (tables and hashed text files, outside `rust/`), so that glob is a `RUST_INPUTS` entry in `tools/ci_select.py`, and `tests/test_ci_select_rust_inputs.py` covers a table-only and a text-only change. The format is in `docs/operator-turns-v1.md`.
