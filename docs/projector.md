# Projector: Temporal history -> keeper (RT-P1-PROJECTOR-CODE)

Design source: rethink-setup design sections 2 ("One owner per fact"), 3 (attempts), 4.5 (exit classes).
Temporal owns execution state and every clock. The keeper stores a projection. The projector is the only writer of that
projection, is idempotent, only moves forward, can be rebuilt from history, and a nightly reconciler flags drift.

Language: Python (`temporalio` SDK, optional import; the keeper never imports it). Package `src/plangraph/projector/`.

## Contract with the Card workflow (v1)

* Plan key: workflow memo field `plan_key` (JSON string), else workflow id `card:<key>` / `card/<key>`. No key -> run skipped
  and counted as `unmapped`.
* Attempt activity: activity type name `attempt` (configurable set `ATTEMPT_ACTIVITY_TYPES`). One workflow-level attempt = one
  activity execution; activity-level retries are off (`maximum_attempts=1`) so every attempt is visible in history.
* Activity result (JSON object, all optional except `exit_class`): `exit_class`, `harness`, `model`, `combo`, `class`,
  `started_at`, `ended_at` (ISO 8601), `tokens_in`, `tokens_out`, `cost`, `log_ref`.
* Workflow result (JSON object): `outcome` in `done | parked | failed`.

### Rust worker conformance (rust/worker)

* The contract stays v1; the WORKER maps, the projector is unchanged. Why: the worker's internal outcomes (`merged`, `parked`,
  `quarantined`, `planner`, `cancelled`) are richer than the projector's `done | parked | failed`; mapping in one pure function
  (`workflow::finish` in `rust/worker/src/workflow.rs`) is the smallest change and keeps the projector free of worker
  vocabulary. Map: `merged` -> `done`; `parked` -> `parked`; `planner` -> `parked`; `cancelled` -> `parked` (the projector
  never sets `cancelled`, that is the keeper verb); `quarantined` -> `failed`. The specific word travels in the extra result
  field `detail` (ignored by the projector).
* Activity type name is exactly `attempt` (activity name override; the other activities are
  `CardActivities::tick|probe|merge|report|park|complete|alarm|janitor|ask_question|record_question_timeout`). `attempt` is scheduled with `maximum_attempts=1`.
* Attempt result carries `exit_class` (always one of `EXIT_CLASSES`; worker `ExitClass` serialises to the same snake_case
  names) plus `harness` (harness name), `model` (leg model), `combo` (leg id), `class` (context window class `128k|256k|1m`),
  `started_at`/`ended_at` (ISO 8601 UTC, millisecond, `Z`). `tokens_in`/`tokens_out`/`cost`/`log_ref` are omitted until the
  harness reports them (the projector treats absent as NULL).
* Plan key: the starter sets memo `plan_key`, or uses workflow id `card:<key>` (the worker's e2e uses the id form).
* A signal-cancelled attempt: the workflow returns `parked` right after the cancel request, so the cancelled activity may not
  close inside the history and then yields no AttemptRec (see Refinements).
* Fixtures and the CI test: `rust/worker/tests/fixtures/projector/*.json` are `temporal workflow show --output json` captures
  of the real e2e runs (the Rust SDK's own history JSON, `tests/fixtures/*.json`, is a different layout used only for replay).
  Re-record with `PG_TEMPORAL_ADDR=... PG_RECORD_DIR=<dir> PG_TEMPORAL_CLI=<path to temporal> cargo test --test e2e` in
  `rust/worker`, the e2e records `<name>.json` (SDK layout, replay fixture, copy to `rust/worker/tests/fixtures/<name>.json`)
  and `<name>.proj.json` (proto-JSON, copy to `rust/worker/tests/fixtures/projector/<name>.json`) with names `happy`,
  `contract_defect`, `check_fails`, `cancel` (1:1 with the fixture files); the checks are
  `tests/test_projector_worker_history.py`. The `check_fails` run may end `parked` or `failed` (quarantined) depending on
  the budget, so after re-recording update the exact expectations in `tests/test_projector_worker_history.py` (`EXPECTED`)
  to the new capture.

## History -> records (`history.py`, pure, no temporalio import, input = proto-JSON history dict)

Input shape: `{"events": [{"eventId": "5", "eventTime": "...", "eventType": "EVENT_TYPE_ACTIVITY_TASK_SCHEDULED",
"<camelCase>Attributes": {...}}]}` (what `WorkflowHistory.to_json()` / `temporal workflow show --output json` give).
Payloads are `{"metadata": {"encoding": b64 "json/plain"}, "data": b64}`; decode json/plain, anything else -> None.

| History | Record |
|---|---|
| ActivityTaskScheduled(type=attempt) | register activity_id; keeper state `running` at that event id |
| ActivityTaskStarted | attempt number (`attempt`, default 1) |
| ActivityTaskCompleted | AttemptRec, exit_class from result (`ok` if the result has none) |
| ActivityTaskFailed | AttemptRec, exit_class = ApplicationFailure `type` if it is in EXIT_CLASSES, else `worker_crash` |
| ActivityTaskTimedOut | HEARTBEAT -> `lease_lost`; START_TO_CLOSE / SCHEDULE_TO_CLOSE / SCHEDULE_TO_START -> `timeout` |
| ActivityTaskCanceled | AttemptRec `cancelled` |
| attempt closed with `needs_input` | state `needs_input`; with `usage_limit` or `outage` -> `waiting` |
| TimerStarted while no attempt activity is open | state `waiting` |
| WorkflowExecutionCompleted | outcome `parked` -> state `parked`; `failed` -> `failed`; else no state (keeper owns done) |
| WorkflowExecutionFailed / TimedOut | state `failed` |
| WorkflowExecutionCanceled / Terminated | state `parked` (never `cancelled`: the keeper `cancel` verb is not the projector's) |
| WorkflowExecutionContinuedAsNew | run status `continued_as_new`, next run id recorded |

The projector never sets `done` or `cancelled` (not in `SETTABLE_STATES`).

Refinements: a StateRec is emitted only when its state differs from the previously emitted state of the same run (no
`waiting, waiting`). A TimerStarted never overrides `needs_input` (the answer wait carries a timer). StateRec event ids: the
ActivityTaskScheduled id for `running`; the attempt's closing event id for `needs_input`/`waiting`; the TimerStarted id; the
workflow-end event id. Attempt fields not in the result fall back to the event times of ActivityTaskStarted (started_at) and
the closing event (ended_at); `attempt` defaults to 1 when no Started event exists (e.g. terminated while scheduled -> no
AttemptRec is emitted for an activity that never closed). `RunProjection.status` in `running | completed | failed | canceled |
terminated | timed_out | continued_as_new`; it also carries `outcome` (workflow result `outcome`, else None) and `next_run_id`
(ContinuedAsNew). A run with no plan key yields plan_key None and no records (status still filled).

Module contracts (so writers can work in parallel):
* `source.py`: `WorkflowRef(workflow_id, run_id, start_time, status)` frozen dataclass (status = Temporal status name upper-case,
  e.g. `RUNNING`, `COMPLETED`); `class HistorySource(Protocol)`: `list_workflows(self, *, open_only: bool = False) -> list[WorkflowRef]`
  (one ref per run, sorted by (start_time, workflow_id, run_id)), `fetch_history(self, workflow_id, run_id) -> dict`;
  `FileSource(dir)` over `tests/projector_record.py` output files. `temporal_source.py`: `TemporalSource` (lazy temporalio).
* `apply.py`: `class Projector(store, principal, *, now=time.time)`; `apply_run(proj, *, run_ord) -> ApplyResult` (counters
  attempts_written, attempts_existing, states_written, states_skipped_state, states_existing, states_stale); module function
  `project_source(source, projector, *, open_only=False) -> dict` (counters + processed/unmapped); `rebuild` is the same call
  (idempotent). Global watermark updated at the end of `project_source`.
* `reconcile.py`: `reconcile(store_or_conn, source, projector_or_none, *, now=time.time) -> ReconcileReport`.
* `run_ord` = index of the run in the workflow's runs sorted by (start_time, run_id).

`project_history(history, *, workflow_id, run_id) -> RunProjection` with frozen dataclasses `AttemptRec(workflow_id, run_id,
activity_id, attempt, scheduled_event_id, closed_event_id, plan_key, exit_class, harness, model, combo, class_, started_at,
ended_at, tokens_in, tokens_out, cost)`, `StateRec(workflow_id, run_id, event_id, plan_key, state, reason)`,
`RunProjection(workflow_id, run_id, plan_key, status, start_time, last_event_id, attempts, states)`. Attempts are ordered by
scheduled_event_id; states by event_id. Deterministic: same history -> equal output.

## Idempotent apply (`apply.py`)

Idempotency key = (workflow_id, run_id, activity_id, attempt). State records use activity_id `ev<event_id>`, attempt 0.
Tables (migration 3, `ddl.py`, PG and SQLite variants, created only through the migration / `db._upgrade` hooks):

* `projector_applied(workflow_id, run_id, activity_id, attempt, kind, key, n, applied_at, PRIMARY KEY (workflow_id, run_id,
  activity_id, attempt, kind))`, `kind` in `attempt | state`.
* `projector_watermark(workflow_id PRIMARY KEY, run_id, run_ord, event_id, status, plan_key, updated_at)`; the row with
  workflow_id `*` is the global watermark (`event_id` unused, `updated_at` = completion of the last full scan). The UI shows it: `GET /v1/projector_watermark` (read-only, same auth gate as `GET /v1/questions`) returns `{global, workflows, open_alarms}` and the inbox page prints one line from it.
* `projector_alarms(id, kind, workflow_id, key, detail, raised_at, cleared_at)`; open = `cleared_at IS NULL`.

Writes go through the keeper `Store` (`record_attempt`, `set_state`) with a `system` principal, never raw SQL on keeper tables.
* Attempt `n` = next number for the key in history order, taken from `projector_applied` order; row fields `workflow_id`,
  `run_id`, `log_ref = "temporal:<activity_id>:<attempt>"`. Crash window (attempt row written, applied row not): on `Refused
  conflict` for that n, if the existing row has the same workflow_id/run_id/log_ref, just insert the applied row.
* State: apply a StateRec only if its (run_ord, event_id) is greater than the stored watermark for the workflow (forward only),
  only if the item exists and its current state is in `SETTABLE_STATES`; else record it applied with no keeper write and count
  `skipped_state`. Terminal (`parked`, `failed`) states are not overwritten by a later `running` from an older run.
* Watermark row updated after each run is fully applied.
* `rebuild`: re-run every workflow from history in (start_time, workflow_id) order. On an existing database it is a no-op
  (idempotent); on a fresh keeper it yields attempts rows equal (ignoring `id`) and the same `projector_applied` set.

## Source, CLI, reconciler

* `source.py`: `HistorySource` protocol (`list_workflows(query)`, `fetch_history(workflow_id, run_id)`), a temporalio-backed
  `TemporalSource` (lazy import; address/namespace from env `TEMPORAL_ADDRESS` default 127.0.0.1:7233, `TEMPORAL_NAMESPACE`),
  and `FileSource` reading history JSON files (tests, offline rebuild).
* `cli.py` / `__main__.py`: `python -m plangraph.projector run-once|run|rebuild|reconcile` with keeper config like the keeper CLI.
* `reconcile.py`: open workflows (visibility `ExecutionStatus="Running"`) vs keeper states. Alarm kinds: `unmapped_workflow`,
  `missing_item`, `state_drift` (projected expected state != items.state), `orphan_running` (item running/waiting/needs_input,
  no open workflow, grace 15 min), `watermark_stale` (global watermark older than 30 min while workflows are open). Alarms
  dedupe on (kind, workflow_id, key) while open and clear themselves when the condition is gone.
  A sixth kind, `unknown_shape`, is raised by `apply` (not by the reconcile pass, which never clears it): an activity type
  outside `KNOWN_ACTIVITY_TYPES` (`attempt` and `CardActivities::tick|probe|merge|report|park|complete|alarm|janitor|ask_question|record_question_timeout`), an
  attempt result payload that is not a JSON object, or a completed attempt with a void result (no `exit_class`). One open row
  per (workflow_id, run_id, activity_id): `key = <run_id>:<activity_id>`, so repeated applies are no-ops. Kind is free text, no
  migration. A completed result that is a JSON object without `exit_class` keeps the documented default `ok` and raises
  nothing (fixture `13_second_attempt_started`). Every recorded fixture raises none (`tests/test_projector_unknown_shape.py`).
  Reconcile runs nightly; the Inbox reads
  `projector_alarms`.

## Tests

Hermetic: recorded history fixtures under `tests/fixtures/temporal/` (recorded once from a local dev server by
`tests/projector_record.py`), parser tests, apply/idempotency/forward-only/rebuild/reconciler tests on SQLite and, when
`PG_KEEPER_BACKEND=postgres`, on Postgres. Live: a test that starts `temporal server start-dev` (binary from env `TEMPORAL_CLI`
or PATH; skipped when missing) and runs real workflows. Migration test: upgrade a v2 Postgres schema to v3.

## Operations

* Exactly one projector process per keeper (`run` or a scheduled `run-once`). The ledger primary key makes a second process lose
  loudly (an integrity error or a `conflict` refusal) rather than corrupt rows; it is not a supported mode.
* Nightly reconcile runs as the systemd user timer `plangraph-projector-reconcile.timer` (03:30 local, up to 10 min random
  delay, Persistent=true) which starts the oneshot `plangraph-projector-reconcile.service`; that runs
  `python -m plangraph.projector --config <keeper.json> reconcile`. Units and install/enable steps: `deploy/systemd/`
  (README.md there). No agent cron. The command exits 0 even when it raised alarms (a mismatch is an open row of
  `projector_alarms`, `cleared_at IS NULL`, that the next pass clears when the condition is gone; read it with
  `plangraph.projector.reconcile.open_alarms`); it exits 1 only on a runtime failure and 2 on bad config, which fails the
  unit. The projector never sets `done`/`cancelled`: closing a card stays a keeper verb.
* Attempts have no dedicated keeper event type; `state_set` events (written by `Store.set_state`) are the keeper events the
  projector produces, the attempt rows themselves are the append-only record of each attempt.
