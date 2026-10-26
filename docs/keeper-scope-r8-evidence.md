# Keeper scope r8 — D-2458

Run: 20261025-035345-scope-r8m-6b8a2f
Writer: mimo/mimo-v2.6-flash 20261025-035345-scope-r8m-6b8a2f
Plan-Item: plangraph:RT-OVERSIGHT-KEEPER-SCOPE

Every finding below is either fixed or refuted, each with a named test that is
red on the base patch (167 of 184 tests in the two affected scope files fail
with `src/` reverted) and green with the fix.

## Seat A (deepseek) + L1 triage

1. **HIGH `store_jev` `NameError` — REFUTED (L1).** `store_jev.py` line 32 is
   `from . import auth, jev_report`; `JevMixin.jev_report` resolves `auth`
   fine. No change.
2. **HIGH `creation_tier` silent clamp — FIXED, contract correction.** The item
   contract is raise-only for oversight: `store_create.creation_tier` now
   raises `403 forbidden oversight:tier_below_default` for an oversight create
   whose explicit tier is below the project default (it never stores the
   default in the item's place), and `oversight_policy._check_create` returns
   the matching verdict — checked *last*, so every specific denial above it
   (scope, fields, kind, criteria, deps) keeps its own reason. A create at or
   above the default is stored exactly as asked. **Tests that asserted the
   silent clamp now assert the refusal** (the contract correction, named in
   the commit message): `test_create_allowed_kinds_and_tiers`,
   `test_oversight_cannot_lower_tier_below_default`,
   `test_same_batch_clamped_high_is_never_an_oversight_target`,
   `test_policy_simulation_uses_all_store_create_defaults`,
   `test_oversight_lowering_prevented_and_raising_allowed`. The honoured half
   is pinned by the new `test_create_at_or_above_default_is_honoured`
   (`demo` defaults to `standard`, so a `standard` create there is applied and
   stored as `standard`). The R1 question/`none` carve-out is unchanged: it is
   a grant, not a lowering, and its assertions were kept.
3. **HIGH `commit_oversight_token` trusts `prepared` — FIXED.** Phase 2 repeats
   every phase-1 check: `PG_OVERSIGHT_LIVE`, `grants(--go)` against the live
   `decisions` row, an expiry inside `now + OVERSIGHT_TOKEN_TTL_S`, and a
   digest this process actually prepared (`prepare_oversight_token` registers a
   fingerprint, commit consumes it once). The reported input — ten-year expiry,
   `D-FAKE` GO, unprepared digest — mints nothing.
   `test_commit_phase_revalidates_and_rejects_a_forged_prepared`,
   `test_commit_ttl_bound_is_enforced_and_a_refused_commit_keeps_the_prepare`.
   The commit phase is still reachable from no verb/MCP route (pinned by
   `test_mint_only_through_the_local_admin_cli`).
4. **HIGH denied `dry_run` wrote no audit/alarm — FIXED.** `_apply_ops_audited`
   records the denial whatever `dry_run` says; only *applied* rows are
   dry-run-proof (which the module docstring already promised).
   `test_oversight_dry_run_checks_the_gate_and_writes_nothing` and
   `test_oversight_stale_base_seq_is_conflict_and_a_denied_row` now pin the
   denied row + alarm for both doors.
5. **MEDIUM hourly cap only consulted inside `_apply_ops` — FIXED (pattern
   sweep).** Both denial writers (`_record_oversight_denied` for batches,
   `_record_oversight_refusal` for the verb seam) consult the denied cap first
   and refuse `oversight:rate_limited` past it without writing, so the
   10,000-call `cancel` loop stops at the cap; `_record_oversight_applied`
   re-reads both caps before every applied row, so one huge batch cannot walk
   past the applied cap either.
   `test_verb_seam_denials_stop_at_the_denied_cap`,
   `test_applied_cap_stops_a_batch_mid_flight`.
6. **MEDIUM dependency targets ran through `_item_reason` — FIXED.**
   `_dependency_reason` now refuses only `dep_unknown`/`dep_cancelled` plus the
   R1/R6 kind and tier bars: a DONE dependency is legal, as the contract and
   `docs/KEEPER.md` say, while the *item* an edit lands on keeps the OPEN
   check. `test_add_dep_to_a_done_dependency_is_allowed`.
7. **MEDIUM `authenticate` ignored `expires_at` — FIXED.**
   `_authenticate_oversight` refuses `oversight:token_expired`; the clock is
   `now=` or the one the store publishes (`auth.set_clock` from
   `Store.__init__`), so a TTL the store stamped is read back by that same
   clock. `test_authenticate_refuses_an_expired_oversight_token`.
8. **MEDIUM unhashable op shapes raised `TypeError` — FIXED.** `check_batch`
   fails closed with a `malformed` verdict (plus explicit non-text `kind` and
   non-text `evidence_kinds` guards) and `store_ops._check_op_shape` refuses a
   non-text op name as `validation`, so neither seam can raise.
   `test_malformed_op_shapes_fail_closed_with_a_verdict` (three shapes).
9. **LOW fold "updates the token ledger" — the doc was wrong, not the code.**
   The mint event is keyless, so `fold` returns before its branch; the
   `oversight_tokens` ledger is revoke-only table state, never an
   event-sourced cache (not a small code change, so the claim was corrected
   and the gap named): `docs/KEEPER.md`, `events.fold` docstring/branch.
   Pinned by `test_fold_does_not_rebuild_the_oversight_token_ledger`.
10. **LOW read project guard ran before `_run` — FIXED.** All four explicit
    filters (`progress`, `needs_you`, `answers`, `jev_report`) run inside
    `_run`, so the token re-check and the denial audit always come first (and
    an out-of-scope refusal is audited too, which it was not before).
    `test_read_project_guard_runs_after_the_token_recheck`.
11. **LOW `CREATE TRIGGER IF NOT EXISTS` never repaired a guard — FIXED.**
    `db._upgrade` drops all four oversight guard triggers and re-runs
    `OVERSIGHT_DDL` on every open, so a weakened body cannot survive a
    reopen. `test_weakened_oversight_guard_triggers_are_repaired_on_open`.
12. **LOW rate-limit denial stored `reason=''` — FIXED.** The budget refusal
    carries an `input_reason` and the writer falls back to the refusal text.
    `test_rate_limited_denial_row_carries_a_human_readable_reason`.

## Extra triage (glm)

* **MEDIUM alarm `ON CONFLICT` index — VERIFIED, nothing to add.**
  `idx_projector_alarms_open` (partial unique on
  `(kind, workflow_id, key) WHERE cleared_at IS NULL`) is declared in the
  SQLite DDL the open path installs *and* in PG migration 3, and is present on
  the live backend; `_upgrade` re-runs the SQLite DDL, so a dropped index is
  recreated too. Pinned by
  `test_open_alarm_unique_index_exists_on_both_schemas` (DDL text for both
  backends, live index + functional duplicate probe on the active backend).
* **MEDIUM one alarm per finding — FIXED.** The guard alarm is keyed by the
  principal (one open `oversight_denied` row however the `finding` ids
  rotate); the finding still rides in the detail.
  `test_rotating_finding_ids_keep_one_open_alarm`.
* **LOW `pgo1_` prefix — FIXED.** `authenticate` resolves the principals table
  first (dispatch on a stored marker) and `add_principal` re-rolls any draw
  that would start with `pgo1_`.
  `test_principal_token_never_dispatches_to_the_oversight_lookup`.

## Validation

* Red-first proof: `src/` reverse-applied to the base patch → 167 failed, 17
  passed in `tests/test_keeper_oversight_scope.py` +
  `tests/test_keeper_tier_principal_scope.py` (every new test red); with the
  fix → 184 passed. No existing test was deleted, renamed or weakened — 82 →
  97 test functions in the scope file, none removed; the five edited tests are
  the ones that asserted the clamp or the pre-fix dry-run behaviour.
* Full suite, `GIT_CONFIG_COUNT`/`GIT_CONFIG_KEY_*`/`GIT_CONFIG_VALUE_*`/
  `GIT_CONFIG_PARAMETERS` unset (their presence makes every `git init`
  subprocess fail in this sandbox): **7126 passed, 34 skipped, 1 failed**.
  The one failure is `tests/test_planner_readiness.py::test_smoke_checks_default_dry_run`,
  which wants a `python` executable on `PATH` (this sandbox has only
  `python3`); with a `python` shim on `PATH` that file is 101/101 green. It is
  not an oversight path and is untouched by this round.
* the Mimo model-path audit (`mimo_path_sweep.py` under `tools/`) → exit 0.
* `python3 tools/split/scrub_check.py . --rules tools/split/manifest.toml
  --rules tools/split/scrub_ci.toml` → **0 hits** (once the generated
  pytest node-id cache is deleted: those *test node ids* read like secrets and
  r7 hit them too).
* `python3 tools/ci_commit_gate.py check --base origin/main --head HEAD` →
  this checkout has **no remote and no `origin/main`** (one `sandbox base`
  commit), so the command as written cannot resolve its base (rc=2). The
  required check was run against the only resolvable base, the sandbox base
  `5086699`, by pointing `refs/remotes/origin/main` at it: trailers, test
  removal and gate-edit rules are green there (rc=0). No `.github/workflows`
  file is touched.
* **Push:** `git push` is refused by this sandbox (permission denied) and no
  remote is configured (`git remote -v` is empty), so this round's commit
  stays local to this checkout (branch
  `scope-r6/20261025-035345-scope-r8m-6b8a2f`, built on sandbox base
  `5086699`).

## Remaining gaps

* **Postgres guard triggers are repaired once, not per open.** Migration 8
  installs them; SQLite repairs them on every open, PG would need a per-open
  drop/recreate in `db_pg.open_pg` (not added: it runs DDL on every open of a
  shared backend and no live PG is reachable in this run to pin it).
* **`oversight_token_minted` is not event-sourced.** Replaying the chain
  cannot rebuild `oversight_tokens` (named in `docs/KEEPER.md`); making it
  rebuildable means changing `fold`'s return contract for keyless events and
  teaching `verify_integrity` about a new cache — out of scope for a doc-claim
  fix.
* **A denied `dry_run` denial counts toward the denied cap** (it is a real
  attempt) and **`authenticate` failures at auth time stay unauditable** in
  `oversight_ops` (an expired token now fails authentication, so the seam row
  only appears for tokens that expired *after* authentication) — both stated
  in `docs/KEEPER.md`.
* **Cap windows are per principal and hourly, not global.** One principal's
  breach does not throttle another's, by design (R4 is alarm-only).
