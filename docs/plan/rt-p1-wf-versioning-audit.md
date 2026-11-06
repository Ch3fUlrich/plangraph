# RT-P1-WF-VERSIONING: acceptance inventory

## Fix-round status (supersedes the original snapshot below)

### PR #536 ruling checkpoint (run-5ba6e566f88e45d6a26a62af2feaf14b)

**Not acceptance-ready. Both seat findings remain open.** This checkpoint does
not substitute a synthetic regression for either required production proof.

- **HIGH / V1 — gap:** `process_crash.rs` still launches the same executable
  twice with runtime `CRASH_BUILD` values. `child_worker` has no
  `deployment_options`. Its test is now explicitly named unversioned/same-binary
  recovery, not new-build or pinned-workflow recovery. Required follow-up: compile
  two separate worker binaries with different compile-time build identities;
  register both with worker deployment versioning and pinned behavior, promote
  the new build, SIGKILL the old process at both crash points, and demonstrate
  that the pinned run completes on a restarted **old-build binary**, not the new
  current build. Assert the executing build identity and bounded completion.
- **MED / V2 — gap:** no test executes `Card::run`'s `input.resume.take()` restore,
  `ctx.history_length()` rollover gating, or queued-answer signal splice. The
  `Resume` wire round trip and synthetic `due()`/Continue-As-New checks are not
  evidence for those branches. Required follow-up: drive a real Card past the
  event cap, or unit-test the production restore/gating boundary and splice;
  assert every restored field, per-run wait baseline, and old-before-new signal
  order. Deleting the production restore block must fail that test.
- **V3 — fail-closed prerequisite retained:** `PG_TEMPORAL_CLI` is unset in this
  worktree environment. The crash acceptance entry point must fail on absence,
  including in CI; it has no implicit skip or green fallback. No local exemption
  is added. The `child_worker` no-address return is only the subprocess entry
  point, not acceptance evidence. No live Temporal or distinct-build result is
  claimed here.

The two implementation slices above are deferred, not silently declared fixed.
This round only corrects the misleading crash-test label and records the exact
remaining acceptance work. It does not enlarge the already oversized production
workflow module or introduce a file-size exemption.

Checkpoint validation: `cargo test --manifest-path rust/worker/Cargo.toml --lib
rollover::tests` passed (2 tests). Running the renamed process-death test failed
as required with `PG_TEMPORAL_CLI ... NotPresent` (1 failed, 0 ignored); this is
negative prerequisite evidence only, not successful crash/routing evidence.

- Done: mandatory explicit deployment/build identity before connecting;
  `identity_is_mandatory_even_without_versioning_switch` covers absent/invalid
  values with the versioning switch unset and rejects opt-out.
- Done: current-version promotion is retained and failure now aborts startup;
  `test_set_current_failure_fails_start` covers the exhausted retry path.
- Done: released `baseline-v1` archive with provenance and checksums, exclusive
  per-build recording, and a merge-base policy checker with edit/delete tests.
  Archive pins current fixtures by hash and copies on change: missing old bytes
  fail Rust/Python pin checks; temporary-file tests cover rejection and retention.
  Replay also runs every kept history under `releases/`; baseline needs no copies.
  Remaining: wire the checker into required CI after Gate-Edit.
- Existing patch guards preserved: `replays_pre_observe_cap_park` and
  `replays_pre_reobserve_cap_park` replay old histories. New command changes
  use `bounded-runs-v1`; runbook specifies patch/version and drain policy.
  Remaining: automated workflow-change policy CI wiring.
- Partial, not acceptance evidence: subprocess test targets a started activity
  and timer, but uses an unversioned synthetic workflow and the same executable.
  Missing CLI now fails rather than passing silently. No real run was obtained
  in the replan environment (Temporal CLI unavailable). Required slice: pinned
  Card workflows, registered distinct deployment builds, explicit routing
  transition, SIGKILL at both crash points and bounded replacement completion.
- Partial: only attempt schedule-to-close is implemented. Synthetic rollover
  exercises commands, not Card state transfer. Required slices: replay-guarded
  bounds at every activity site; Resume restoration and queued-signal splice;
  Card event/history and per-run wait-cap rollover with preserved attempt,
  budget, question and base state. These clauses remain unproven.
- Done: operational runbook in `rt-p1-wf-versioning-runbook.md`.
- Remaining: two independent non-Claude review seats; no agents requested.

Mutation probes failed for identity defaults, ignored promotion, archive overwrite,
shifted caps, extended retry timeout and SIGTERM; all restored before final checks.

## Replan review disposition

Root causes are evidence validity, incomplete replay-safe options/state coverage,
archive enforcement scope, bootstrap lifecycle, and helper/API documentation drift.
The fixtures policy now rejects edits/deletions throughout the fixtures tree,
even if an archived copy is added; required CI wiring remains blocked by the
Gate-Edit boundary below. Identity fallback APIs were removed rather than kept
as a second, contradictory identity policy.

Unclosed findings: distinct pinned-build crash recovery, production Card Resume
coverage, all-activity bounds, fresh-stack promotion, duplicate archive tests and
I2/D7 comment restoration. The latter files, plus workflow.rs and devstack.sh,
already exceed 400 lines. No new exemption was added; splitting these production
and integration-test modules and validating historical replay needs a dedicated
slice rather than another unverified local patch. This PR is not ready on these
acceptance clauses. The file-size gate must be reported as failing if it fails,
not interpreted as permission to enlarge grandfathered modules.

## Original pre-implementation snapshot

Audited local `origin/main` at
`b4a2751eb8fd7ada22a7b59ef921502c2347b671`; no remote refresh or push.
This is an evidence checkpoint, not completion of the keeper item.

## Done-clause inventory

| Clause | Main evidence and remaining gap |
| --- | --- |
| Refuse startup without deployment/build identity | Missing: `rust/worker/src/buildid.rs::from_env` returns `None` for `PG_VERSIONING=off` or invalid identity; missing deployment and build overrides default rather than fail. |
| Start sets new build current | Already partly present: `tools/devstack.sh::start_set_current` invokes `set-current-version`; `tests/test_devstack_sh.py` covers it. Failure only warns, and the documented recovery still permits a stalled queue. Preserve existing promotion rather than duplicate it. |
| Append-only released-build replay fixtures, CI rejects edits | Offline replay exists in `rust/worker/tests/replay.rs`; pre-observe and pre-reobserve parking histories are explicitly checked. Recording in `tests/e2e.rs::record` still targets named fixtures, not a released-build archive. No append-only CI check found. |
| Every workflow change has patch or new version plus drain note | Markers already exist in `workflow.rs`: gate, observe, and reobserve guards. The claim that main has no `patched()` is stale. No general PR policy check found. Preserve existing markers and old parking arguments. |
| SIGKILL mid-attempt and mid-timer, new build completes | Existing `e2e.rs::restart_worker` aborts an in-process poll task and recreates a worker; it is not process SIGKILL or a different-build restart. CI runs a local Temporal dev server but lacks the required two crash scenarios. |
| Activity schedule-to-close | No `schedule_to_close` configuration found in `rust/worker/src`; still required. |
| Continue-As-New above event/wait cap | No `continue_as_new` implementation found in `rust/worker/src`; projector fixtures mentioning it are not worker implementation. |
| Runbook | `docs/plan/rtp1-live-run.md` already documents manual current-version promotion. General patch/drain, archive, crash recovery, and rollover procedures remain required. |
| Two non-Claude seats | Not obtained in this run; no agents spawned, consistent with the run rules. No independent review claimed. |

## Authorization stop: exact proposed door hunks

`tools/ci_commit_gate.py::AUTH_DOOR_PATHS` includes `.github/*`
(Python fnmatch matches nested workflow paths). No Gate-Edit D-id was supplied.
Implementation stops before editing any door path, including:

- `.github/workflows/tests.yml`: add an always-run, merge-base-aware frozen
  replay archive check rejecting edits, removals, and renames of released data.
- `.github/workflows/tests.yml`: wire a workflow-change check requiring a
  recognized patch marker or explicit new version with a drain note; include
  the check in aggregate required CI dependencies.
- `.github/workflows/tests.yml`: wire hermetic local-Temporal process tests
  that SIGKILL during an attempt and during a durable timer, start a new build,
  enforce bounded completion, and capture failure diagnostics.

After authorization, remaining slices are strict identity and promotion;
released-build archive plus policy checker; bounded activity retries and
replay-safe rollover; subprocess crash tests and CI wiring; operational
runbook and two independent non-Claude reviews. Each implementation slice must
respect the 400-line file/change budget and preserve historical replay data.
