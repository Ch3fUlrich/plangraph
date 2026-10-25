# Keeper scope r7 — D-2458

Run: 20261025-010901-scope-r7-14797a
Writer: cx-gpt-6.1-sol

## Findings

1. Fixed cycle attribution: `test_cycle_refusal_writes_a_denied_row_and_alarm`
   failed red with HOST/op 1; now pins OTHER/op 2, the cycle-closing operation.
2. Fixed persisted-default simulation: store creation and policy simulation use
   `_normalise_create`, including `_tier_for`. Red-first
   `test_same_batch_clamped_high_is_never_an_oversight_target` covers edit,
   add_dep and priority. `test_policy_simulation_uses_all_store_create_defaults`
   compares the entire normalized definition: kind omitted, project defaults,
   title escalation and the question/none exception.
3. Fixed duplicate mint lifecycle and generation race: direct mint delegates to
   prepare/commit; generation is allocated under the write lock. Red-first
   `test_prepared_mints_allocate_generation_only_at_commit` prepares two tokens
   before either commit and requires successive generations.
4. Refuted as a required read-isolation change: the item grants scoped writes,
   not isolated reads. Corrected the overbroad test/comment claims.
   `test_oversight_read_scope_is_not_a_general_isolation_contract` explicitly
   pins ordinary snapshot visibility. Existing
   `test_oversight_read_verbs_are_project_scoped` covers four explicit project
   guards only. This is not a claim of general read isolation.
5. Confirmed, not refuted: judge, D-825 GO, and two independent non-Claude
   review-seat approvals are unavailable in this run. This patch does not mean
   the HIGH item is done, approved, merged, or live-authorized. External
   approvals cannot truthfully be replaced by a unit test.

## Validation

- Required pytest command, GIT_CONFIG_COUNT unset: 2689 passed, 25 skipped
  (168.84s). Final focused scope rerun: 163 passed.
- `mimo_path_sweep.py .`: exit 0.
- Public scrub: 0 hits after removing the generated pytest node-id cache;
  initial cache matches were credential-shaped test names.
- Required ci_commit_gate base 0a65abac2bc8d7f69326dd3db44d7fb3edbb90f5
  is absent from this checkout's object database. That gate is not green.
