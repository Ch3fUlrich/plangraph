# RL29 reconciliation red acceptance tests

Adds four ignored Linux tests for design §4/invariants 2, 3, 5 and 6;
no production behavior changes. Temporary populated confinement fixtures contain
a real live child PID. All children are killed/reaped on unwind. No real cgroup
root is used. Persisted clock input is injected at the original deadline rather
than waiting ten seconds; inventory is reopened across restart/re-key probes.

Run: `cd rust && cargo test -p pg-spawnd -- --ignored rl29_`

On current origin/main production code (unchanged on this lane), all four fail
in `rust/pg-spawnd/src/reconcile_store_rl29_tests.rs` at their first exact
`Ok(false)` expectation: `.unwrap()` rejects `Err(Unknown)`:

- `rl29_dead_leader_populated_saved_group_reclaims_after_grace`: line 127,
  `assert!(!reconcile(&dir).unwrap(), "populated hold, not reclaim")`.
- `rl29_zombie_live_member_holds_grace_then_reclaims`: line 170,
  `assert!(!reconcile(&dir).unwrap(), "live zombie-group member holds")`.
- `rl29_stale_identity_without_confinement_proof_becomes_unresolved`: line 225,
  `assert!(!reconcile(&dir).unwrap(), "expired uncertainty retains capacity")`.
- `rl29_identity_rekey_preserves_run_slot_hold_deadline`: line 262,
  `assert!(!reconcile(&dir).unwrap(), "re-key retains capacity")`.

Every reconcile call requires an exact boolean success, including restart and
terminal replay; Unknown is never accepted. Populated tests require unresolved
with retained capacity, then explicitly kill/reap members and write the fixture's
events to `populated 0` before requiring repair reclaim and durable settlement.
The zombie is observed with blocking `waitid(WEXITED | WNOWAIT)`, not timed
polling. Re-key coverage requires reconcile-written `observed_identity` and
`observed_attempt` evidence in the proposed sidecar schema; merely preserving
seeded anchor values cannot pass. No production schema change is made here.

Normal crate tests pass with these four skipped; clippy and formatting pass.
The proposed sidecar schema is test input, not an implementation of recovery.
