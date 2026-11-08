# RL29 crash reconciliation state model

D-5054 step 1 of 3: design note only; step 2 adds failing tests; step 3 implements the fix.

Plan: `plangraph:RL29-RECONCILE-DESIGN`; router D-5054; replan-0390.
Design contract for implementation seats and judge; no runtime changes here.
Baseline: `origin/main:rust/pg-spawnd/src/reconcile_store*.rs`.
References only: `origin/lane/rl29-reconcile-crash` and
`origin/lane/rl29-settle-probe`, inspected with git show/diff, not checked out.
Twelve fix rounds without a shared model motivate this explicit safety boundary.
Reference grace-expiry settlement of populated groups is rejected, not endorsed.

## 1. Complete observation/action table

The four axes give **108 states** (3 records × 3 identities × 3 leaders × 4 groups).
The eight disjoint rows below cover every combination; `*` means every axis value.
Record: P = present/running attempt, M = missing record or attempt, T = terminal.
Identity: V = recorded PID/start-time matches, S = mismatch/reused PID, U = unknown.
Leader: A = alive/non-zombie, Z = zombie, D = dead/absent.
V with D means previously verified identity with independently verified death;
an unverified disappearance is U. A reused PID is S/A, never the owned leader.
Group: P = populated, E = empty, A = absent, U = unreadable/unprovable.
E/A require authenticated run/attempt confinement provenance and a fresh probe;
untrusted paths, reused PGIDs, permission errors and missing identity-only evidence
are U, not A. Identity staleness does not invalidate independently owned confinement.
Zombie-only groups remain P until reaped/removed and the authoritative probe is empty.

| Row | Record | Identity | Leader | Group | States | Action | Required before settled |
| --- | --- | --- | --- | --- | ---: | --- | --- |
| S1 | * | V | A | P | 3 | Hold; durably transfer to reattach/watch owner with the persisted deadline and terminal-unresolved + alert obligation intact, or cleanup on policy deadline | Owner observes exit, empties group, then S5–S7; unresolved at the unchanged deadline otherwise |
| S2 | * | V | Z/D | P | 6 | Cleanup owner kills remaining owned members; hold; reclaim only after S5–S7 | Fresh `gone() == Ok(true)`, not leader death or grace expiry |
| S3 | * | S/U | * | P | 18 | Hold + alert; cleanup only with independent confinement authority | Owned group emptied and original leader non-live; otherwise unresolved |
| S4 | * | * | A | E/A | 18 | Hold + alert: inconsistent evidence or unrelated reused PID; resolve ownership | Prove recorded leader non-live and group empty/absent; do not kill reused PID |
| S5 | P | * | Z/D | E/A | 12 | Settle, then reclaim | Locked identity re-read + fresh `gone() == Ok(true)`; durable settlement first |
| S6 | M | * | Z/D | E/A | 12 | Persist recovery tombstone, then reclaim | Saved owned identity/group proof + fresh `gone() == Ok(true)`; absence of record alone is insufficient |
| S7 | T | * | Z/D | E/A | 12 | Preserve terminal outcome; reclaim idempotently | Same fresh proof as S5; terminal status alone is insufficient |
| S8 | * | * | * | U | 27 | Hold + alert; repair/probe within bound, otherwise unresolved | No settlement until authoritative group proof and non-live original leader |

Actions are transitions, not synonymous results: **settle** commits a proven-gone
outcome; **hold** retains ownership/capacity; **reclaim** releases capacity only
after proof and durable disposition; **alert** records an operator-visible event.
S1–S4/S8 always enter the bounded-hold protocol below, including missing/terminal
records. A changed identity/attempt under the record lock restarts classification,
not the hold clock. Terminal unresolved is not S7's proven-gone disposition.

## 2. Who empties the group and who proves it

| Component | Responsibility | Proof / restriction |
| --- | --- | --- |
| Reconcile coordinator (`reconcile_serve` / store) | Classify under state lock; re-read under record lock; assign cleanup/watch owner | `orphan_gone` is a proof/commit boundary, not a timer-based success shortcut |
| Reattach/watch owner | Monitor verified live run; enforce existing runtime/reattach deadlines; request cleanup | Handoff is durable and explicit; it cannot silently discard a hold |
| Backend cleanup owner | Kill all owned cgroup members (Linux), terminate owned job (Windows), or signal authenticated process group (PGID fallback) | Never signal a stale PID/PGID solely because its number matches; no authority means unresolved |
| Backend probe via `gone()` | Re-probe after termination; wait for exit/reaping and drain | Linux authoritative `cgroup.events populated 0` or authenticated absence; Windows empty job or proven absence; PGID requires authenticated ownership and whole-group absence, not leader mismatch |
| `orphan_gone` / settlement caller | Commit only after fresh whole-group and original-leader proof; then release slot | `Ok(true)` implies no living original leader or member; errors/permission denial are not emptiness |
| Parent/subreaper and OS | Reap zombies; backend waits until authoritative empty/absent | A zombie leader does not excuse a living child; zombie-only uncertainty remains bounded hold |

Killing the leader does not empty a group. Sending a kill request does not prove
completion. Probe and commit must share the ownership fence: prevent new members
or identity reuse between proof and commit (held job/scope authority, or equivalent
PGID ownership fence). If no such fence is available, hold then unresolved.
No settlement or capacity release while any owned member lives, regardless of age.

## 3. Bounded holds and terminal unresolved

These are proposed persistence/API additions, not claims about origin/main.
Use `UNKNOWN_IDENTITY_GRACE_MS = 10_000` in `reconcile_store`'s hold helper for
**every reconcile hold**, not just U identities; the reference branch has this
constant but its expiry-to-settlement behavior is not the contract.

| Item | Durable location / transition | Bound and crash behavior |
| --- | --- | --- |
| Hold anchor | `runs/<run_id>/reconcile.json`, keyed by stable run/slot ID, not attempt or identity fingerprint: `hold_started_ms`, `hold_deadline_ms`, `reason`, `disposition`; attempt/fingerprint are evidence only | Atomically save before returning the first hold; set deadline once = first hold + constant; retries/restarts/record rewrites and identity/attempt re-keys reuse this anchor and never reset the deadline or create a second hold |
| Missing record/attempt | Same sidecar/tombstone; discover holds independently of `inventory()` running records | Retain identity and group provenance; missing `record.json` cannot erase the clock |
| All S1–S4/S8 holds | At deadline, re-probe once; proven gone follows S5–S7; otherwise persist `disposition=unresolved` | Terminal recovery state, not `settled`, not repeated `Err(Unknown)`; S1 may transfer responsibility through an explicit durable watch handoff before deadline only if the same persisted anchor/deadline and terminal-unresolved + durable alert obligation transfer with it; handoff neither renews the budget nor exempts the new owner from that ending |
| Unresolved alert | Sidecar outbox event `RECONCILE_UNRESOLVED`, key `(run_id, slot_id, hold_started_ms)` (stable ids only, no attempt: a re-key must not mint a second alert; two slots never collide); mirror in record when available | Atomically persist disposition + event, replay delivery idempotently after crash; include evidence and cleanup failure |
| Capacity/late repair | Keep slot or durable quarantine reservation while unresolved; operator/cleanup repair may later prove gone | End automatic hold retries at deadline; no unproved reclaim; subsequent repair records a separate resolution |
| Storage/lock failure | Do not acknowledge disposition; retain reservation, surface service-health alert | Deadline remains due; retry durable write on recovery, never reset clock or claim settlement |

Use a nondecreasing effective clock persisted with the hold; clock rollback must
not renew its remaining budget. Forward jumps may end hold early, never prove gone.
If persistence itself is unavailable, terminal durability cannot be guaranteed;
that is a surfaced service failure, not an infinite logical `Unknown` policy.

## 4. Open judge findings → required red tests

Tests below are required acceptance changes, not tests added by this docs commit.

| Finding | Table rows | Failing test / required assertion |
| --- | --- | --- |
| P5 dead-leader reclaim must assert group emptied | S2/S3 → S5–S7, never populated → reclaim | Strengthen reference `reconcile_store_settle_probe_tests::dead_leader_populated_saved_group_reclaims_after_grace`: use an owned real group/member; assert member exited and authoritative group empty **before** reclaim; if cleanup unavailable assert unresolved + retained reservation. Fake `populated 1` alone must never pass settlement. |
| No live zombie-group member after `orphan_gone` returns `Ok(true)` | S2/S3 | Reverse reference `reconcile_store_settle_probe_tests::zombie_live_member_holds_grace_then_reclaims`'s live-child assertion: success requires child exited/reaped and `gone() == Ok(true)`; a surviving child requires non-success, then bounded unresolved. Test must fail on reference success-with-live-child behavior. |
| Stale identity without confinement proof reaches bounded end | S8 (also S3/S4 if evidence contradicts) | Add `reconcile_store_identity_tests::stale_identity_without_confinement_proof_becomes_unresolved`: reused PID + absent/untrusted PGID, restart across persisted deadline; assert terminal unresolved + exactly one alert, no signal to reused PID, no settlement/reclaim, and no further automatic `Err(Unknown)` loop. |
| Identity re-key cannot renew or duplicate a hold (invariant 5) | S1–S4/S8 | Step 2 adds `reconcile_store_identity_tests::identity_rekey_preserves_run_slot_hold_deadline`: change attempt/identity fingerprints repeatedly, including across restart; assert one stable run/slot anchor, the original start/deadline unchanged, no second hold, and terminal unresolved + one durable alert at that deadline if still unproven, with capacity retained. |

## 5. Numbered invariants for seats and judge

1. Every observation maps to exactly one of S1–S8; record absence/terminality is not process proof.
2. `settled` and `orphan_gone == Ok(true)` require no live owned leader/member and fresh fenced whole-group proof.
3. Leader death, PID reuse, zombie status, grace expiry and successful kill submission never substitute for `gone()`.
4. Only an authenticated cleanup owner kills members; stale numeric identities never authorize signals.
5. Each stable run/slot ID has one hold anchor and one persisted deadline, set once at first hold, including missing/terminal records and unreadable groups. Identity/attempt re-keys cannot reset that deadline or create a second hold; they only update evidence and restart classification (step-2 test: `identity_rekey_preserves_run_slot_hold_deadline`).
6. Expired uncertainty becomes terminal unresolved + durable idempotent alert, never fictional settlement or endless `Unknown`.
7. Unresolved capacity stays reserved/quarantined; proven disposition precedes reclaim, including crash replay.
8. State/record locking, identity re-read and ownership fencing protect probe-to-commit; changed evidence is reclassified.
9. Settlement/outcome, alert delivery and reclaim are idempotent across crash/restart; preserve existing terminal outcomes.
10. All finding tests assert process/group facts, durable disposition and capacity, not merely return codes.

Open questions: none for this contract. Implementation seats must select the concrete
cross-platform ownership fence and sidecar schema/API wiring without weakening it;
unsupported proof paths terminate unresolved rather than assuming emptiness.
