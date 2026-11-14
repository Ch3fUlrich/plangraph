3. NEW-CODE-GATE-ALLOWLIST-OPERATOR-ONLY: Require operator authority for allowlist additions

Why: OPERATOR D-5912 (2026-11-14T09:15Z), following rule 9.14 / D-5847:
self-approved D-5840 exemptions admitted Python in #881 head_audit.py, #896
retired_models.py/model_admission.py and base-fresh-gate base_fresh.py.
How: Port the gate to rust/pg-policy with a compatibility Python launcher.
Read allowlist policy from HEAD and operator authority from the merge-base
index; reject additions without an OPERATOR D-id, including D-5840/D-5830.
Remove the superseded broad Python/shell exemptions without adding any entries.
The spawner pack and brief template carry the language rule; the new
PY-TO-RUST-MIGRATION-PLAN lists the gate port and all first-port files.
Done: Rust git-history regressions reject D-5840/D-5830 and self-authored
index authority, accept OPERATOR D-5912 and pure removals; the legacy Python
gate failed the same regression first. Existing Python parity tests are retained.
Merge and CI run after this no-push worktree handoff.
