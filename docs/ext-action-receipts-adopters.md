# External-action receipts: the verbs each adopter uses

Design only. This note names the receipt model's verbs for every external write of five
adopters: LAND-ONE-MERGE-AUTHORITY, LAND-ROLLOUT-SHADOW-CUTOVER, SPAWN-IDEMPOTENCY-KEY,
PLACEMENT-R7B-BROKER and SPAWN-LAUNCH-RECOVERY-DESIGN (done). It adds no protocol. Where
a row needs something the model does not give, the gap is an open question (section 8).
Item: `plangraph:EXT-ACTION-RECEIPTS-ADOPTER-VERBS`. Design of record: plan-roadmap
`ext-action-receipts-design-20261121.md` v3, sections 2 and 3.

## 1. The verbs

The reference model is `rust/pg-receipts/src/ledger.rs` (`pg_receipts::Ledger`). Each
successful call is one serialized keeper transaction.

| Verb | Signature | What it does |
| --- | --- | --- |
| `begin` | `begin(id, spec, children)` | Persists the intent before any effect. The same id with the same spec and manifest returns the stored state; a different spec or manifest is `Conflict`. A `conflict` key or `binding` shared with a live row is `Conflict`. `children` are the manifest (rows `Planned` until the parent is `Ok`). |
| `admit` | `admit(ticket)` | The reservation step. Only for `Gated`, `Launch` and `Protected` rows in `Dispatching`; refused after the ticket deadline, after a cancel request, on a stale generation or a superseded ticket. Moves the attempt to `Reserved`, the row to `Admitted`. |
| `effect` | `effect(ticket)` | Records the external effect (`Reserved` to `Applied`). Refused once a cancel is requested. |
| `takeover` | `takeover(generation, predecessor_fenced)` | A successor owner takes the rows. Refused unless the predecessor is fenced, and while a gated row is `Dispatching` or a `Protected` row has an attempt that is neither `Applied` nor `Fenced`. |
| `read` | `read(id, credential, scoped_successor)` | The state of one receipt for the holder of its credential, or for the scoped successor. The only retry read path. |
| `obligations` | `obligations()` | Ids that still need an owner: `Pending` and `Unknown` rows, expired `Dispatching` or `Admitted` rows, due `Planned` children, due `Failed` children without a replacement. |

Transitions that move a row between these verbs are `Ledger::apply(id, stamp, Step)`
in `rust/pg-receipts/src/transition.rs`: `Dispatch` (returns the `Ticket` that `admit`
takes), `Expire`, `Observe(Proof)`, `Bind`, `Resolve`, `RetryChild`, `RequestCancel`.
`fence`, `confirm_stopped` and `deliver` (non-gated classes only) are also in
`ledger.rs`. The tables name `apply` steps where a row needs one; they are not a seventh
verb of the adopters (open question O1).

Column meanings in the tables below:

- **Write**: one external write the adopter makes.
- **Receipt id / binding**: the invocation id the caller persists, `Spec.class`,
  `Spec.conflict`, `Spec.binding`, `Spec.credential`.
- **Site**: where `begin`, `admit`, `effect` run, as `path::Symbol` on main, or
  `not built yet: <planned site>`.
- **Takeover**: what a successor does after a crash.
- **Retry read**: what a retry reads first.
- **§3**: the requirements of section 2 below that the row satisfies (`L*`, `S*`); `-`
  means none, `gap` means a requirement it should satisfy and does not yet.

## 2. Section 3 of the design, numbered

Landing (LAND-ONE-MERGE-AUTHORITY, LAND-ROLLOUT-SHADOW-CUTOVER):

- **L1** A land is one receipt with binding (repository, ref, full head, GO D-id) and a
  manifest of its follow-through.
- **L2** The authority itself validates GO, tested head and base before dispatch.
- **L3** An ambiguous push stays under the same authority, lease not released, until
  its outcome is proven or its expected old value is definitively invalidated.
- **L4** App-only exclusivity is a condition of the rollout's cutover check.
- **L5** merge-part, batch land, the land wrapper and `phase_finish` call the authority
  or stay disabled for protected refs.
- **L6** The Temporal worker's ref path is covered before that path is enabled
  (executor in force: pg-spawnd, D-6530).
- **L7** Shadow proof and cutover check are separate done checks.

Launch, cancel, feeder (SPAWN-LAUNCH-RECOVERY-DESIGN, SPAWN-SESSION-ADMISSION-DESIGN,
SPAWN-IDEMPOTENCY-KEY, PLACEMENT-R7B-BROKER):

- **S1** The caller persists invocation id, target and claim epoch before dispatch.
- **S2** The launcher calls `admit` in the step that reserves, before the subprocess starts.
- **S3** The feeder releases a claim or tries another target only after `expire` committed.
- **S4** A cancel is its own receipt linked to the launch.
- **S5** `cancel_requested` is persisted before any kill; `admit` and replacement
  recovery honour it.
- **S6** Tests: a pause between validation and reservation across the deadline; cancel
  before process creation; a daemon dying mid-cancel.
- **S7** (PR tools and CI rerun) belongs to slice S4 of the design, not to these five
  adopters; it is out of scope here.

## 3. LAND-ONE-MERGE-AUTHORITY

The designated merger (the land App) is the only actor that updates protected main.
One land is one receipt of class `Protected`. `authority_at_destination` is `true` only
when the destination (the ruleset plus `--force-with-lease=refs/heads/main:<B>`) enforces
the same authority (open question O2). The ids below are hashes of the named fields.

| Write | Receipt id / binding | Site | Takeover | Retry read | §3 |
| --- | --- | --- | --- | --- | --- |
| Land of a train or batch pull request: the tested-ff push of main, or the pull request merge | id: land invocation id, persisted by the caller (`D-id` + head). `Protected`; conflict `(repo, ref)`; binding `(repo, ref, full head, GO D-id)`; credential = the authority's | begin: not built yet: the land authority entry, before it validates GO, tested head and base (L2). admit: not built yet: the authority, after validation, just before the push. effect: not built yet: the authority, on the push reply. Nearest today: `tools/merge_batch_land.py::land_with_lease` (`landing_journal` acquire/release), `tools/merge_batch_build.py::landing_lease` | `takeover` is refused while the row's attempt is neither `Applied` nor `Fenced`, so the successor first resolves it: `apply(Observe(Proof))`, `Present` when main holds the head (`tools/merge_batch_build.py::read_verified_merge` is that probe today), `FencedAbsent` only when the expected old value is definitively invalidated. The lease stays held meanwhile | `read(id, credential, scoped_successor)`; a retry never re-pushes while the state is `Dispatching`, `Admitted` or `Unknown` | L1, L2, L3 |
| Follow-through of a land (manifest children): keeper settlement, worktree and branch cleanup, close-merged, telemetry row, rerun record | one child id per step, passed in `begin`'s `children`; each `Planned` until the parent is `Ok`; class `Gated` | begin: with the parent. admit and effect: not built yet: each step, e.g. `tools/merge_batch_land.py::settle_train`, `tools/merge_batch_util.py::cleanup_landed`, `tools/merge_batch.py::cmd_landing_record`, `rust/plangraph-cli/src/merge_close.rs::run_with` | the successor reads `obligations()`: due `Planned`, `Failed` without replacement; `takeover` re-stamps non-settled children | `obligations()`, then `read` per child | L1 |
| `phase_finish` merge call | caller of the authority, same receipt id as the land of the phase | not built yet: `src/plangraph/planner/phase_finish.py::_finish_locked` sends the request to the authority instead of `github.merge_pr` (`github_adapter.py::RestGitHub.merge_pr`, expected head only) | as the first row | as the first row | L5 |
| Batch land and the land wrapper | callers of the authority, same receipt id | not built yet: `tools/merge_batch_land.py::cmd_land`, `rust/pg-spawnd/src/land_go_shell.rs::Shell::land` (via `land_go.rs::handle`) request the land instead of merging | as the first row | as the first row | L5 |
| merge-part (router wrapper) | caller of the authority | not built yet: its merge call is removed or routed; this repository holds only its mode reader, `tools/merge_batch.py::cmd_landing_mode` | as the first row | as the first row | L5 (O3) |
| Temporal worker merge into the main ref | caller of the authority, or disabled for protected refs | not built yet: `rust/worker/src/activities_merge.rs::merge_fenced` is covered or disabled for protected refs before enabling; its present guard is a local fencing token (`rust/worker/src/mergelock.rs::admit_fence`), not a keeper receipt | as the first row | as the first row | L6 (O4) |
| Reads of the cutover checks (proof-ref (1) to (10), App-only bypass, required `land-auth` check) | no write | `read` only; the authority refuses to `begin` a land when a check fails | - | - | L4 |

## 4. LAND-ROLLOUT-SHADOW-CUTOVER

| Write | Receipt id / binding | Site | Takeover | Retry read | §3 |
| --- | --- | --- | --- | --- | --- |
| Shadow receipt of a computed train: an inert row kind with no GO-HEAD anywhere on its line | id: shadow id per train and head; `Gated`; no `binding` that equals a live land's | begin only: not built yet: the shadow builder (`rust/plangraph-cli/src/land/train.rs`, `build.rs`). It is never dispatched, so no `admit` or `effect` exists for it, and the live gate and merge-part refuse it | none: an inert row has no effect to recover; a successor re-`begin`s the same spec and gets the stored state | `read`; compared with routing's row, any difference is a defect item | L7 |
| Routing's GO row (step 1, in force) and the land it triggers | row text with `D-id`, PR, head; the `<D-id> <head>` pair is the receipt key | today: `rust/pg-spawnd/src/land_go.rs::pass` writes the done line after `handle`; `handle` calls `Landing::land` and `Landing::post`. Under the authority: the first row of section 3 | today a crash between `land` and the done-line append handles the row again and leans on `refusal` (head moved, behind main, checks); under the authority the successor `read`s the land receipt | `read` of the land receipt; the done file is not a receipt (O5) | L1, L5 |
| Publishing the `land-gate` and `land-auth` check status on a head | id: `(repo, head, CI attempt)`; the destination is GitHub; class `Foreign` | not built yet: the check publisher; nearest reader today: `rust/plangraph-cli/src/land/main_gh.rs::answer_for` | `apply(Observe(Proof))` against the status list; a status already present for that attempt is `Present` | `read`, then the status list | L7 (O6) |
| Cutover: change main's ruleset so `land-gate` is required and the lander is the only merger | id: ruleset change `(repo, ref, ruleset version)`; `Protected` | begin: not built yet: the cutover step. admit: only after the proof-ref checks (1) to (10) pass and the red-main repair proof exists. effect: the ruleset write | refused while the write is neither `Applied` nor `Fenced`; the successor reads the live ruleset to prove it | `read`, then the live ruleset | L4, L7 |
| A routed red-main repair (EXCEPTION:main-red-fix row) | the land receipt of section 3 whose `binding` includes the exception row's D-id | as the first row of section 3; the authority validates the row before `begin` | as the first row of section 3 | as the first row of section 3 | L1, L2 |
| Defect item for a shadow difference (keeper `create`) | id: the `idempotency_key` of SPAWN-IDEMPOTENCY-KEY | row W3 of section 5 | row W3 of section 5 | row W3 of section 5 | - |

## 5. SPAWN-IDEMPOTENCY-KEY

The key is the invocation id: the client allocates it before the first call and reuses it
on every retry. A repeat returns the stored outcome and starts nothing.

| Write | Receipt id / binding | Site | Takeover | Retry read | §3 |
| --- | --- | --- | --- | --- | --- |
| W1 `plangraph_agent_spawn` creates a run and its worktree | id: `idempotency_key`; `Launch`; conflict `(caller, key)`; credential: the caller's token; the stored result is the run id | begin: not built yet: `rust/pg-spawnd/src/spawn.rs::spawn`, after `leg_ref::admit` and `validate` and before `runs.spawn`. admit: not built yet: the reservation step inside `rust/pg-spawnd/src/runs.rs::Runs::spawn`, before the subprocess starts. effect: not built yet: after the process exists. The native path is `rust/pg-spawnd/src/placement.rs::native_spawn`; same receipt | a daemon that restarts reads `obligations()`; a run with an `Admitted` attempt and no process is settled by `apply(Observe(..))` against the run record (`crate::records::read_record`) | `read(key, credential, None)`; `Pending` or `Unknown` returns "outcome unknown" and never a second run | S1, S2 |
| W2 `plangraph_session_launch` starts a numbered session | id: `idempotency_key`; `Launch`; conflict `(role, session name)`; the stored result is the session name | begin: not built yet: `rust/pg-spawnd/src/session/mod.rs::launch`, after `Arguments::parse`. admit: at the existing `starting` row, `storage::ledger(.., "starting", ..)` in `session/mod.rs::local`, which must run before the child. effect: after the child exists, `session/storage.rs::publish`. Remote: `session/remote.rs::launch` forwards the key | the recovery model owns this: section 7 | `read`; a `Dispatching` or `Admitted` row means "do not retry", as `remote.rs::launch` already prints for an uncertain forward | S1, S2 |
| W3 keeper `create` of an item | id: `idempotency_key`; keeper-local; the item key is the stored result | not built yet: `src/plangraph/keeper/store_create.py::create_items` / `_create`, begin and effect in the write boundary of the verb. The keeper-local receipt store exists: `src/plangraph/keeper/store_outbox.py` (`write_receipt`, `_receipt_transaction`) | none: mutation and receipt commit together, so there is no uncertain interval (design S1) | `read`; a repeat returns the stored item key | - (O7) |
| W4 a repeat older than 24 hours | the receipt is past its retention window | the retention rule of the receipt store: not built yet | a new key's receipt is a new row; the old one stays readable until reaped | `read` returns the old receipt or nothing | - (O8) |

## 6. PLACEMENT-R7B-BROKER

The three credentials map to `Spec.credential`: the submission capability (issued at the
session's item claim) is the credential of the row, so `read` is caller-scoped by
construction. Rows are the `spawner_jobs` rows of
`src/plangraph/keeper/schema_pg_spawner.py` (primary key `(run_id, attempt_no)`, unique
live index on `run_id`, `payload_hash`, `generation`, `cancel_requested`).

| Write | Receipt id / binding | Site | Takeover | Retry read | §3 |
| --- | --- | --- | --- | --- | --- |
| Job submit (`POST jobs`) | id: `(run_id, attempt_no)`; `Launch`; conflict `run_id`; payload hash bound once with `apply(Bind(payload))` (a `Bind` of another payload is refused, O9b) | begin: not built yet: the broker submit route (table `spawner_jobs`, `schema_pg_spawner.py`); persists id, target and claim epoch before dispatch | a retry with the same spec returns the stored state through `begin`; a different payload hash is refused by `Bind` with `Refused::Guard`, not `Conflict`, and the retry's `read` settles the outcome | `read(id, submission credential, None)` | S1 |
| Job claim by the daemon (`jobs/poll`) | the execution lease token is the `Ticket` of `apply(Dispatch)`; `generation` is the stamp | dispatch: not built yet: the atomic claim in the poll route. admit: not built yet: `rust/pg-spawnd/src/placement.rs`, in the step that reserves, before the subprocess (`native_spawn`) | a lease that is not admitted by its deadline is `Expire`d; only then may the run be re-placed (S3) | `read`; `obligations()` lists expired `Dispatching` and `Admitted` rows | S1, S2, S3 |
| Job start effect and result (`status/record POST`, `wait`) | the finalization authority is bound to the submission: row `spawner_finalizations` (`finalizer_id`, `generation`, `expected_old`, `publication_state`) | effect: not built yet: the status/record route; the result publication reads `spawner_finalizations.publication_state` | a successor finalizer takes `generation + 1` through `takeover`; a `publishing` state with `expected_old` is settled by `Observe` | `read`, caller-scoped | S2 |
| Conditional withdraw | a cancel of a job that is still `queued` | not built yet: the withdraw route. The model gives `apply(RequestCancel)` (class `Launch` only) then `Observe(FencedAbsent)` once the conditional update proves `queued` to `withdrawn` | the withdraw is one transaction under the run-row lock; no interval | `read` | S4, S5 (O9) |
| Cancel (`cancel POST`) | its own receipt, `cancels` the job: `apply(RequestCancel(invocation))`; `spawner_jobs.cancel_requested` is the persisted flag | not built yet: the cancel route writes `cancel_requested` and the cancel row in one transaction before any kill; daemon side today: `rust/pg-spawnd/src/jobs.rs::cancel_run` kills through `supervisor.cancel` with no persisted request first | after the process exists the cancel stays open until `confirm_stopped`; a successor reads the cancel row | `read` of the cancel receipt | S4, S5, S6 (gap today) |
| Re-place and resolve | lock the run row; one live attempt (`idx_spawner_jobs_live`); a replacement is a new `attempt_no` | not built yet: re-place in the broker, after `Expire` of the old attempt and `Resolve` of the unknown one | `takeover` does not refuse an `Admitted` `Launch` row (it refuses only a gated `Dispatching` row or an unsafe `Protected` row, `ledger.rs::takeover`), so the single live attempt rests on the `Expire` then `Resolve` order and on `idx_spawner_jobs_live`, not on `takeover` | `obligations()` | S3 (O10, O13) |
| Host probe (`hosts probe POST`; GET is a read) | keeper-local upsert of `spawner_hosts` (`host_id`, `probe`, `updated_at`); id: `(host_id, probe digest)`; no external effect, not a receipt write | none: begin and effect are the one upsert transaction (`schema_pg_spawner.py`, table `spawner_hosts`); the route is not built yet | none: the upsert is idempotent per `host_id`; a later probe replaces the earlier | `read` is not needed; the GET returns the stored row | - (O14) |

## 7. SPAWN-LAUNCH-RECOVERY-DESIGN (done)

The protocol is the executable model `rust/pg-launch-recovery-model` (events `Reserve`,
`Forward`, `Receive`, `TakeStart`, `StartChild`, `LaunchOutcomeUnknown`, `Publish`,
`MirrorAndClose`) and `rust/pg-session-admission-model`. It was built before the receipt
model and uses its own events; this table maps each event to the ledger verbs. A launch
is class `Launch`, with `resends = 0` ("launch uncertain; do not retry",
`session/remote.rs::launch`).

| Write | Receipt id / binding | Site | Takeover | Retry read | §3 |
| --- | --- | --- | --- | --- | --- |
| Full launch spec and the `starting` row with the whole identity, before Claude starts | id: the launch invocation id; `Launch`; conflict `(host, role, session name)`; binding = the spec digest | begin: not built yet: `rust/pg-spawnd/src/session/mod.rs::launch`, before `restart::prepare`; today only `storage::ledger(.., "starting", ..)` in `session/mod.rs::local` exists. admit: not built yet: that `starting` write, before `StartChild`. effect: after the child exists, `session/storage.rs::publish` | the watchdog or a re-run `takeover`s only after the predecessor is fenced; one matching session is adopted, an ambiguous match is refused | `read` by the invocation id; `Unknown` stays `Unknown` without evidence | S1, S2 |
| Origin-side reservation before forwarding over ssh | the same id; `Reserved` to `Forwarded` in the model | not built yet as a receipt: `session/mod.rs::launch` before `session/remote.rs::launch` | the origin's `read` decides; a forward whose reply was lost is `Unknown`, never resent (`resends = 0`) | `read`, then the listing evidence at the target | S1, S2 |
| Forward and run at the target (`remote.rs::send`, `transmit`) | the same id travels in the spec; the target `Receive`s once per digest | effect at the target: `session/mod.rs::local` through `remote.rs`; a stale or name-taken receive is refused | per-attempt staging of remote artifacts: a late session cannot pass the newer attempt | `Observe(Present)` or `FencedAbsent` from the listing, `Undecidable` leaves `Unknown` | S2 |
| Mirror of remote records and close of the reservation | a manifest child of the launch (`MirrorAndClose`) | child of `begin`: `session/remote.rs::mirror` | due once the parent is `Ok`; `obligations()` lists it | `read` of the child | - |
| Watchdog adoption or stop of a launch | linked to the launch; `apply(RequestCancel)` for a stop | not built yet in Rust: the watchdog on this protocol (`docs/liveness-watchdog.md` describes today's) | the launch row stays re-stamped to the successor while the process is not stopped; `confirm_stopped` after the process is gone (`takeover` itself does not wait for it, O13) | `read` of the cancel receipt | S4, S5 |
| Session stop and run cancel (`plangraph_session_stop`, `plangraph_agent_cancel`) | their own receipts linked to the launch | not built yet: the receipt before the kill; run cancel today: `rust/pg-spawnd/src/jobs.rs::cancel_run` | as the cancel row of section 6 | `read` of the cancel receipt | S4, S5, S6 |
| Feeder claim and start (`claim`, `start`, `release`) | the claim epoch plus the invocation id, persisted before `start` | `rust/plangraph-cli/src/feeder/mod.rs::spawn` claims, starts, and on any start error calls `release` at once; `adapters.rs` `Launch::start` shells out to the spawner CLI with no key | a release or another target only after `Expire` committed | `read`, then `obligations()` | S1, S3 (gap today) |

## 8. Open questions

Nothing below is decided here; each needs the design owner or the model owner.

- **O1** The brief's six verbs omit `apply` (`Dispatch`, `Expire`, `Observe`, `Bind`,
  `Resolve`, `RequestCancel`), `fence`, `confirm_stopped` and `deliver`. Section 3 itself
  says `expire`. Should the adopter designs name `apply` steps as verbs, or does
  `ledger.rs` gain `dispatch` and `expire` wrappers?
- **O2** The `authority_at_destination` flag of a land: true only if the ruleset plus the
  lease check enforce the authority at the destination. Who sets it, and from which
  proof?
- **O3** merge-part's merge call is not in this repository; this repository has only its
  mode reader. Where is it retired?
- **O4** The Temporal worker's merge ref path has a local fencing token
  (`mergelock.rs::admit_fence`) with no keeper receipt. Disable for protected refs, or
  route to the authority, before it is enabled?
- **O5** The land row's done file (`land_go.rs::pass`) is written after the merge. A
  crash between them re-handles the row. Is the done file replaced by the land receipt?
- **O6** A status publication to GitHub has no model class that is gated. `Foreign` uses
  `deliver`, not `admit` and `effect`. Is `Foreign` right for a check status?
- **O7** Keeper `create` is a keeper-local write, covered by S1 of the design, not
  section 3. Which receipt table does it use: the existing `op_receipts`, or a new one?
- **O8** The model has no retention window. How does "a replay older than 24 h starts a
  new one" map to `begin`, which returns the stored state for the same id?
- **O9** Withdraw of a never-claimed job: `RequestCancel` exists only for class
  `Launch`, and `Observe(FencedAbsent)` on a `Pending` row requires a cancel, a
  disposition or an attempt. Is this the intended reading? **O9b**: payload binding is `Step::Bind`, which refuses
  a different payload with `Guard`. Does the broker want `Conflict` (payload hash in
  `Spec.binding`) instead?
- **O10** `Ledger::takeover` is global (one ledger generation). The broker's `generation`
  is per job row, and re-place is per run. Does a per-run takeover exist, or does a
  broker restart take over all rows?
- **O11** The inert shadow row (section 4) is read as "begun, never dispatched". The
  model has no inert class. Confirm the reading, or name the class.
- **O12** Receipt ids are `u32` in the model. The production id is a string invocation id
  or a digest. Where is the mapping fixed?
- **O13** `Ledger::takeover` does not refuse an `Admitted` `Launch` row or wait for
  `confirm_stopped`. If the broker needs that rule (single live attempt across a
  takeover), it is a model change; the broker must not assume it.
- **O14** The host probe write is read as a keeper-local, idempotent upsert that needs no
  receipt. Confirm, or give it an id and a retention rule.

## 9. Requirement coverage

| Requirement | Adopter rows that satisfy it |
| --- | --- |
| L1, L2, L3 | section 3 rows 1 and 2; section 4 rows 2 and 5 |
| L4 | section 3 row 7; section 4 row 4 |
| L5 | section 3 rows 3 to 5; section 4 row 2 |
| L6 | section 3 row 6 |
| L7 | section 4 rows 1, 3 and 4 |
| S1, S2 | sections 5 and 6 (W1, W2, job rows); section 7 rows 1 to 3 |
| S3 | section 6 claim and re-place rows; section 7 feeder row (gap today) |
| S4, S5 | section 6 withdraw and cancel rows; section 7 watchdog and stop rows (gap today) |
| S6 | section 6 cancel row; section 7 stop row; the tests belong to each adopter's done check |

Every `not built yet` site is a planned site, not a commitment of a file name. When
plan-order copies a table into its keeper item, the open questions it names go with it.
