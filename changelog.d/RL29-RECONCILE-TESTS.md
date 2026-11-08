# RL29 reconciliation red acceptance tests

Adds four ignored Linux tests for design §4/invariants 2, 3, 5 and 6;
no production behavior changes. Temporary populated confinement fixtures contain
a real live child PID. All children are killed/reaped on unwind. No real cgroup
root is used. Persisted clock input is injected at the original deadline rather
than waiting ten seconds; inventory is reopened across restart/re-key probes.

Run: `cd rust && cargo test -p pg-spawnd -- --ignored rl29_`

On origin/main production code, all four fail at
`rust/pg-spawnd/src/reconcile_store_rl29_tests.rs:65`:
`assert_eq!(hold["disposition"], "unresolved", ...)` sees `hold` instead.

- `rl29_dead_leader_populated_saved_group_reclaims_after_grace`: live member
  prevents proof; unavailable cleanup must terminate unresolved with reservation.
- `rl29_zombie_live_member_holds_grace_then_reclaims`: same bounded fallback;
  any successful settlement must instead prove member exit and empty group.
- `rl29_stale_identity_without_confinement_proof_becomes_unresolved`: reused
  PID survives, but current code leaves overdue uncertainty nonterminal.
- `rl29_identity_rekey_preserves_run_slot_hold_deadline`: fingerprint/attempt
  rewrites preserve seeded timestamps, but never produce terminal disposition.

Normal crate tests pass with these four skipped; clippy and formatting pass.
The proposed sidecar schema is test input, not an implementation of recovery.
