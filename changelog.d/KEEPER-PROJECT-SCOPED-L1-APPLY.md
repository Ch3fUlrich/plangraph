A role-l1 principal with a router-written `apply_grants` row may now `apply_ops` (and
`tools/apply_batch.py`) in its granted projects: create, edit, cancel, priority,
add_evidence, close_on_decision and legacy_state. One key outside the granted projects
refuses the whole batch and names it; rehome, set_kind, dependency ops and any grant,
role or token change stay router-only. Each applied batch posts one report line to
`routing`. New verbs `grant_apply`, `revoke_apply`, `apply_grants` (`pg grant-apply`);
PostgreSQL migration 13.
A granted batch keeps the l1 name and role in its events, a cancel inside it may not rewrite a
dependent in a foreign project, the grant check resolves aliases in the lock, and a granted
token refuses `--per-op`.
Bare references in a granted edit qualify with the project of the resolved target, and
`--per-op` needs a verified identity even with `--allow-no-whoami`.
