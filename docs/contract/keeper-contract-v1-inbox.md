# Keeper contract 1.0 - inbox extension (RT-P1-INBOX-CODE)

Additive routes over the existing `questions` / `answers` tables and the
`Store.ask_question` / `Store.answer_question` verbs. They are **not** part of the
frozen 43-route `keeper-contract-v1.json`. All `/v1/` routes need
`Authorization: Bearer`; run tokens are refused with 403. Refusals use the usual
`{"error": code, "msg": ...}` mapping.

## `GET /v1/questions`

Query: `state=open|answered|timeout|all` (default `open`; anything else is
`validation`), `key=<project:ID>`, `project=<name>`. Any authenticated principal;
`router`, `system` and `reader` see every project, other roles only the projects
in their principal. Audited like other reads.

```json
{"questions": [{"id": 1, "key": "demo:Q1", "project": "demo", "text": "...",
  "default_answer": null, "deadline": null, "risk_tags": [], "state": "open",
  "created_at": "2026-01-01T00:00:00Z",
  "answer": null}]}
```

`answer` is `null` or `{"ts", "actor", "text", "source"}`. Rows are ordered by
`id` ascending.

## `POST /v1/ask_question`

Body `{key, text, default_answer?, deadline?, risk_tags?}`. Returns
`{"id", "key", "state": "open"}`. Roles, the no-default rule for risk tags
(`auth`, `secrets`, `money`, `live-step`, `public-release`) and the
default-needs-deadline rule are enforced by the store.

## `POST /v1/answer_question`

Body `{question_id, text?, source?: "answer"}`. Returns the store result
(`question_id`, `state`, `source`, `text`) plus
`"signal": {"status", "workflow_id"}`. The signal is sent only after the store
commit; a signal failure never fails or rolls back the answer.

## `POST /v1/resignal_question`

Router or system only (else 403). Body `{question_id}`. The question must be
`answered` or `timeout` (an `open` one is `bad_state`). Re-sends the signal for the
stored answer and returns `{"question_id", "signal": {...}}`. Audited as
`resignal_question`.

## `GET /v1/alarms`

Any authenticated principal, audited as `alarms` like the other reads, with the
same visibility rule as `GET /v1/questions`: `router`, `system` and `reader` see
every project's alarms, other roles only the alarms whose `key` project (the part
before the first `:`) is in their principal's projects — so a keyless alarm (an
empty `key`, which has no project) is visible to `router`, `system` and `reader`
only. Lists the projector alarms that are still open: `projector_alarms` rows with
`cleared_at IS NULL` that have no successful `ack_alarm` audit row
(`audit_log.action = 'ack_alarm'`, `ok = 1`, `key = 'alarm:<id>'`), ordered by `id`.

```json
{"alarms": [{"id": 4, "key": "demo:DEMO-1", "kind": "state_drift",
  "workflow_id": "wf-1", "detail": "...", "raised_at": "2026-01-01T00:00:00Z",
  "age_s": 90}]}
```

`age_s` is whole seconds from `raised_at` to `store.now()`, or `null` when the
timestamp cannot be parsed. When the query itself fails (a missing or unreadable
table), the reply is still `200` but carries
`{"alarms": [], "error": "alarms unreadable: <ExceptionClass>"}` — never a silently
empty list, and never message text or a path.

## `POST /v1/ack_alarm`

Router or system only (else 403). Body `{alarm_id}`: an integer, not a boolean or
string (else `validation`). The alarm must exist (`not_found`) and must not be
cleared (`bad_state`); acknowledging an already acknowledged alarm is idempotent
(a second audit row, no error). The acknowledgement is stored as the successful
`ack_alarm` audit_log row keyed `alarm:<id>` that the store writes on success —
no schema change. A refusal is audited with `ok = 0` and does not hide the
alarm. The reply is `{"alarm_id", "acked": true}`.

## Acknowledgement storage

The acknowledgement is the successful `ack_alarm` `audit_log` row keyed
`alarm:<id>`; no new table, no new column, no migration — the schema is unchanged.

## `GET /inbox`

Static, unauthenticated HTML form (not under `/v1`). Served with
`Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'`,
`X-Frame-Options: DENY`,
`Cache-Control: no-store` and `X-Content-Type-Options: nosniff`. It carries no
data; the page calls the routes above with a token the user pastes in.

## Temporal signal

Configured only through the environment, read at call time:

| Variable | Meaning |
|---|---|
| `PLANGRAPH_TEMPORAL_HTTP` | Base URL of the Temporal HTTP API (http or https). Unset, empty or non-http(s): status `disabled`, no network. |
| `PLANGRAPH_TEMPORAL_NAMESPACE` | Namespace, default `default`. |

The workflow id is `workflow_id` of the newest `attempts` row (highest `n`) of the
question's key that has one. The signal is
`POST {base}/api/v1/namespaces/{ns}/workflows/{workflow_id}/signal/answer` with one
`json/plain` payload of `{question_id, key, text, source}`, `identity`
`plangraph-keeper` and `requestId` `plangraph-q<question_id>-<source>`, timeout 2 s,
no retries.

Signal statuses: `sent` (2xx), `not_found` (404), `failed` (other HTTP error,
network error or timeout), `disabled`, `no_workflow`, `skipped_timeout`
(`source: question_timeout`; the workflow's own timer already fired). The URL, any
token and any response body are never returned or logged.

The keeper runs no question deadline timer; the deadline timer lives only in
Temporal, and the keeper's reaper is untouched.
