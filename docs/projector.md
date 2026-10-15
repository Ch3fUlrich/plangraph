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
  workflow_id `*` is the global watermark (`event_id` unused, `updated_at` = completion of the last full scan). The UI shows it.
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
  dedupe on (kind, workflow_id, key) while open and clear themselves when the condition is gone. Runs nightly; the Inbox reads
  `projector_alarms`.

## Tests

Hermetic: recorded history fixtures under `tests/fixtures/temporal/` (recorded once from a local dev server by
`tests/projector_record.py`), parser tests, apply/idempotency/forward-only/rebuild/reconciler tests on SQLite and, when
`PG_KEEPER_BACKEND=postgres`, on Postgres. Live: a test that starts `temporal server start-dev` (binary from env `TEMPORAL_CLI`
or PATH; skipped when missing) and runs real workflows. Migration test: upgrade a v2 Postgres schema to v3.
