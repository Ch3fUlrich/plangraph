# Recorded Temporal histories (projector fixtures)

Real histories from a local Temporal dev server, recorded by `tests/projector_record.py` (a test
`CardWorkflow` plus an activity named `attempt`, task queue `proj-fixtures`, `maximum_attempts=1`).
Each file is `{"workflow_id", "run_id", "history": <WorkflowHistory.to_json() as a dict>}`.
They are hermetic inputs for the projector parser and apply tests; the recorder is not collected by pytest.

Regenerate (needs `temporalio` and a server; default address `127.0.0.1:17233`, namespace `default`):

    TEMPORAL_ADDRESS=127.0.0.1:17233 python tests/projector_record.py --out tests/fixtures/temporal

Regenerating changes run ids and event times; commit only when the history shape needs to change.

Scrubbing: every `identity` becomes `worker`, `stackTrace` is emptied, home paths and `x@y` strings are
neutralised, and the recorder fails if any host-shaped value, path or non-loopback IP remains. `eventId`
and `eventTime` are kept. `buildId` is a content hash of the recorder module (harmless).

| File | Workflow id | Scenario |
|---|---|---|
| `01_ok_single` | `card:DEMO-1` | one attempt, `exit_class` ok, outcome `done` |
| `02_check_fail_then_ok` | `card:DEMO-2` | attempt 1 `check_fail`, attempt 2 ok |
| `03_usage_limit_wait_then_ok` | `card:DEMO-3` | `usage_limit`, 1 s durable timer, then ok |
| `04_needs_input_signal` | `card:DEMO-4` | `needs_input`, workflow waits for signal `answer` (a timeout timer is started and cancelled), attempt 2 ok |
| `05_app_failure` | `card:DEMO-5` | activity raises non-retryable `ApplicationError(type="contract_defect")`; workflow returns `parked` |
| `06_heartbeat_timeout` | `card:DEMO-6` | silent activity, `heartbeat_timeout` 1 s -> `TIMEOUT_TYPE_HEARTBEAT`; outcome `failed` |
| `07_start_to_close_timeout` | `card:DEMO-7` | `start_to_close_timeout` 1 s -> `TIMEOUT_TYPE_START_TO_CLOSE`; outcome `failed` |
| `08_cancelled` | `card:DEMO-8` | workflow cancelled while the activity heartbeats (wait-cancellation-completed) |
| `09_continue_as_new_run1`, `_run2` | `card:DEMO-9` | run 1: attempt 1 then continue-as-new; run 2 (memo carried over): attempt 2, `done` |
| `10_workflow_failed` | `card:DEMO-10` | one ok attempt, then workflow raises `ApplicationError` |
| `11_unmapped` | `misc-1` | no memo, id without `card:` prefix, one ok attempt |
| `12_terminated` | `card:DEMO-12` | terminated while the activity is scheduled |
| `13_second_attempt_started` | `card:DEMO-13` | activity result without `exit_class` (defaults to `ok`) |

Server facts worth knowing: with a retry policy the server writes `ActivityTaskStarted` only when the
activity closes (so `12_terminated` has `ActivityTaskScheduled` but no Started); in `08_cancelled` the
Started event follows `ActivityTaskCancelRequested`.
