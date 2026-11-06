# RT-P1-WF-VERSIONING: blocked implementation audit

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
