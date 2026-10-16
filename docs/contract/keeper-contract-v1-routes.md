# Keeper contract 1.0 - route reference

Part of `keeper-contract-v1.md` (read its sections 1-5 first: transport, error
codes, roles, state machine). Schema names in brackets, e.g. [Claim], are
`$defs` in `keeper-contract-v1.json`. Every route needs
`Authorization: Bearer <token>` except `GET /v1/health`; a missing or bad
token is `401 forbidden`. "Errors" lists the refusals beyond the common ones:
`400 validation` (bad or missing field), `403 forbidden` (role/scope),
`404 not_found` (unknown key). `key` is always `project:ID`; a key that was
rehomed resolves to its new key on every route.

Query parameters common to reads: `fmt=json|text`, `project=<name>`.

## Reads (GET)

### `GET /v1/health`
Open. `200 {"status":"ok"}` [Health].

### `GET /v1/whoami`
Any role. `200 {name, role, projects[], family|null}` [WhoAmI].

### `GET /v1/ready`
Any role. Query: `n` (default 10), `all` (any value: no limit), `project`,
`owner`, `mine` (`1`/`true`: items whose `owner` is the caller), `agent`
(`1`/`true`: drop operator items). Default text; `fmt=json` gives
`{items: [Item], unblocks: {key: count}}` [Ready]. Items are `open` with every
dependency `done`, ordered by priority desc, `unblocks` desc, `estimate_h`,
key. Errors: `400` for a non-integer `n`.

### `GET /v1/show`
Any role. Query `key=K`, or the path form `GET /v1/show/K`. Default text. `fmt=json`: the item with `depends_on:[{key,state}]`,
`unblocks[]`, `evidence[{kind,verified,at,detail,ref}]`, `reviews[{reviewer,
family}]`, `fixed_by[]`, `run_ids[]`, `created_at`, `done_at`, `reviewed`,
`merged`, `waits_on_operator` [Show]. `brief=1`: the short brief [Brief]
(`key,title,state,kind,owner,review_tier,evidence_kinds,priority,depends_on,
description,done_criteria,note?`). Errors: `404`.

### `GET /v1/blocked`
Any role. Default text. `fmt=json`: `{key: [unmet dependency keys]}` for open
and claimed items that have unmet dependencies [Blocked].

### `GET /v1/waves`
Any role. `[[keys]]`: topological layers of non-terminal items, wave 0 is
ready now [Waves].

### `GET /v1/critical_path`
Any role. `{keys[], hours}`: longest remaining path by `estimate_h` [CriticalPath].

### `GET /v1/percent`
Any role. `by=estimate|count` (default `estimate`). A JSON number 0..100
[Percent]. Errors: `400` for another `by`.

### `GET /v1/alerts`
Any role. Default text. `fmt=json`: `{lease_expiring[], review_overdue[],
findings[{ts,key,code,msg}], refusals[{key,count}], stalled, warnings[{code,
key,msg}]}` [Alerts]. Findings include `changes_requested_twice` and
`review_overdue`.

### `GET /v1/digest`
Router only. `{projects: {name: {open,claimed,in_review,done,cancelled,
stalled}}, done[last 10], waivers[{key,decision_id[,state]}]}` [Digest].

### `GET /v1/snapshot`
Any role. [Snapshot]: `{generated, head, head_seq, items[], aliases{}, activity[],
decision_text{}, progress{}}`. `head` is the 64-hex hash of the newest event
(`null` if none), `head_seq` its `seq`. See contract section 8.

### `GET /v1/aliases`
Any role. Default text. `fmt=json`: `[{old,new,ts,actor,reason}]` left by
`rehome` [Aliases].

### `GET /v1/needs_you`
Any role. Non-terminal operator items (`operator` flag or kind `question`/
`decision`/`operator_task`), most blocking first: `[{key,project,title,kind,
state,wait,priority,unblocks,choices}]` [NeedsYou]. `project` filter.

### `GET /v1/answers`
Any role. Answered question/decision items, newest first: `[{key,project,
title,kind,answer,decision_id,done_at[,superseded_by]}]` [Answers].

### `GET /v1/progress`
Any role. `{projects:[{project,total,done,hours_*,pct_*,states{},phases[]}],
total{}}` [Progress]. `project` filter.

### `GET /v1/changelog`
Any role. Required `since` = an event `seq` (entries with a greater seq) or a
date / ISO datetime (entries at or after it, UTC). Default text. `fmt=json`:
`{since, head_seq, entries:[{seq,ts,type,key,title,by,...}]}` with `type` one
of `created`, `closed` (`via` review|decision), `answered`, `cancelled`,
`restart` [Changelog]. Errors: `400` when `since` is missing or malformed.

### `GET /v1/audit-meta`
Router (all projects) or l1 (own projects; another project is `403`). Query
`project`, `stale_days` (positive integer, default 3). Default text.
`fmt=json`: `{now, stale_days, projects:[{project, missing[], stale_groups[],
stubs[],...}], totals{}}` [AuditMeta]. Errors: `400` for a bad `stale_days`.

## Writes (POST, JSON object body)

Common write errors: `404` unknown key, `409 locked` on a DONE/cancelled item,
`409 epoch_stale` when a fenced verb carries an old `epoch`. Fenced verbs
(`heartbeat`, `release`, `submit`, `add_evidence`) require the current `epoch`.

### `POST /v1/create_items`
Router, or l1 for its own projects. Body `{items:[ItemDef]}` or a bare array
[CreateItems]. ItemDef: `project`, `id` required; `kind`, `title`, `description`,
`depends_on[]` (bare ids mean same project), `evidence_kinds[]`, `review_tier`,
`done_criteria`, `owner`, `track`, `estimate_h`, `priority`, `operator`, `host`,
`wait`, `note`, `owner_family`, `choices`, `kg_refs`, `fixes`, `corrects`,
`layer`, `role`, `agent_id`, `phase_kind`; other fields fold into `note`.
`200 {created:[keys][, warnings:[...]]}` [Created]; `warnings` appears when a
description is missing. State starts `open`. Errors: `400 validation|cycle`,
`403` (project scope, cross-project dependency, tier), `409 conflict|locked`
(key exists). Atomic: any failure creates nothing.

### `POST /v1/import_items`
Router. Same body; honours `state` = `open|claimed|in_review` (claimed items get
epoch 1 and `claimed_by = owner`). `done`/`cancelled` are `400`. [Created].

### `POST /v1/edit_definition`
Router or l1 of the project. `{key, fields:{...}}`; editable: `title,
description, host, wait, layer, role, agent_id, phase_kind, fixes, choices,
kg_refs, estimate_h, done_criteria, evidence_kinds, depends_on, track, owner,
note, review_tier`, and `priority` (router only). `200 {key}` [KeyOnly]
(`{key, priority}` when only `priority` was sent). Only `open` items are
editable (`400 bad_state`); the router may change `review_tier`/`evidence_kinds`
of a `claimed`/`in_review` item (this voids earlier verdicts). Lowering a tier
is router only. Errors: `400 validation|cycle`.

### `POST /v1/set_priority`
Router. `{key, n}` (integer, `priority` accepted as an alias of `n`).
`200 {key, priority}` [Priority].

### `POST /v1/claim`
l1 or worker scoped to the project. `{key[, lease_s]}`; `lease_s` is clamped to
60..86400. `200 {key, epoch, lease_until}` [Claim]. Errors: `400 bad_state`
(not open, or dependencies unfinished), `409 conflict` (already claimed;
exactly one of concurrent claimers wins), `403`.

### `POST /v1/delegate`
The claimant. `{key, worker}` where `worker` is a worker principal scoped to the
project (`400 validation` otherwise). `200 {key, delegate}` [Delegate].
Errors: `400 bad_state` (not claimed), `403`.

### `POST /v1/heartbeat`
Claimant, delegate, or the item's writer run token. `{key, epoch}`.
`200 {key, epoch, lease_until}` [Claim]; the lease only grows. `400 bad_state`
unless `claimed`.

### `POST /v1/release`
Claimant or router. `{key, epoch}`. `200 {key}` [KeyOnly]; the item is `open`.

### `POST /v1/add_evidence`
Claimant, delegate or writer run token. `{key, epoch, kind, ref}` (`kind`
`commit` is `403`). `200 {key, kind, verified, detail, ref}` [Evidence]; an
unverified row is still `200`. `400 bad_state` unless `claimed`.

### `POST /v1/add_evidence_router`
Router. `{key, kind, ref}`; item must be `claimed` or `in_review`. [Evidence].

### `POST /v1/submit`
Claimant, delegate or writer run token. `{key, epoch}`.
`200 {key, epoch, round, state:"in_review"}` [Submit]. Errors:
`400 evidence_missing|evidence_unverified|bad_state`.

### `POST /v1/verdict`
Reviewer (or reviewer run token). `{key, ok: true|false[, reason]}` - `ok` must
be a JSON boolean. `200 {key, state, reviews_passed}` [Verdict]: `state` is
`done`, `in_review` (first `high` verdict), `claimed` (changes requested) or
`open` (second consecutive change request). Errors: `400 bad_state|
family_unknown|evidence_*`, `409 same_principal|same_family|conflict`.

### `POST /v1/cancel`
Router, or l1 of the project (`409 conflict` while an active item depends on
it). `{key[, reason]}`. `200 {key, state:"cancelled"}` [Cancel]. Allowed from
`open`, `claimed`, `in_review`; otherwise `400 bad_state`.

### `POST /v1/reap`
System role only; every HTTP token of another role gets `403`. Expires lapsed
leases. `200 {expired[], overdue[]}` [Reap].

### `POST /v1/waive_second_family`
Router. `{key, decision_id}`: the decision row must name the key in `Waives:`.
Only `high` items (`400 bad_state` otherwise). `200 {key, state,
review_waiver}` [Waive]. `400 evidence_unverified` for an unknown decision or
one that does not name the key.

### `POST /v1/close_on_decision`
Router. `{key, batch_ref: "<path>@<sha256>"}`. The item must be `claimed` or
`in_review` with tier `none`, carry verified `decision` evidence (and commit
evidence if listed) whose `Closes:` tokens name the key, and the batch file must
lie in a configured batch root with a matching digest. `200 {key,
state:"done", decision_ids[], sha256}` [Close]. Errors: `400 validation|
bad_state|evidence_missing|evidence_unverified`, `403` (outside batch roots).

### `POST /v1/set_legacy_state`
Router. `{key, state: "claimed"|"in_review"[, note]}` on an `open` item; epoch
+1, `claimed_by` = the item's `owner`, no lease. `200 {key, state, claimed_by}`
[Legacy]. `400 bad_state` unless open.

### `POST /v1/apply_ops`
Router. `{ops:[Op], batch_ref?}`, 1..500 ops, all or nothing (contract section
7). `200 {applied: N, results:[...]}` [ApplyOps]. A refusal returns the failing
op's status and code with `msg` = `op <n> <op> <key>: <code> <message>`.

### `POST /v1/migrate_descriptions`
Router. No fields. `200 {migrated:[keys], skipped:[{key,reason}]}` [Migrate];
moves folded `description:` note lines into the column of active items.

### `POST /v1/rehome`
Router. `{key, new_project[, host]}` on an `open` item. `200 {old, new, host,
repointed[]}` [Rehome]. The old key is cancelled and aliased to the new one;
unlocked dependents are re-pointed. `400 validation|bad_state`, `409 conflict`.

### `POST /v1/record_run`
Spawner. `{key, run:{run_id, phase:"started"|"finished"[, rc, writer:{model,
family,source}, seats:[{model,verdict,run_id}]]}}` on a `claimed`/`in_review`
item. `200 {key, run_id, phase, writer_model, seat_models, run_ids}`
[RecordRun]; repeating the same `(run_id, phase)` returns `{..., duplicate:
true}` and appends nothing.

### `POST /v1/answer`
Router. `{key, text, decision_id}` for a `question`/`decision` item that is
`open`, `claimed` or `in_review`; the decision row must name the key in
`Closes:`. `200 {key, state:"done", decision_id}` [Answer].

### `POST /v1/mint_run_token`
Router, or l1 of the project. `{key, role:"writer"|"reviewer", run_id}`.
`200 {key, role, run_id, epoch, expires_at, token}` [MintToken]; the token is
shown once. Errors: `400 bad_state|evidence_unverified|validation`.

### `POST /v1/replay`
A run token only (`403` otherwise). `{entries:[{id (32 hex), verb, body}]}`,
1..100 entries; verbs `heartbeat`, `add_evidence`, `submit` (writer token) or
`verdict` (reviewer token), executed in order with the same checks as live
calls. `200 {results:[{id, result}]}` [Replay]; `result` is `ok`, a refusal
code, or `duplicate` (with `first`) when the `id` was already seen.

### `POST /v1/withdraw_review`
Router. `{key, decision_id: "D-<n>"}` for an `in_review` item whose evidence
kinds include `decision` (or whose done-criteria ask for no commit); the
decision must name the key in `Closes:`. `200 {key, state:"claimed", epoch,
lease_until}` [Withdraw]. Same epoch, fresh lease, reviewer cleared.

### `POST /v1/reopen`
Router. `{key, reason: "<=500 chars", decision_id: "D-<n>"}` for an `in_review`
item; the decision only has to be recorded (no `Closes:` naming).
`200 {key, state:"claimed", epoch, claimed_by, lease_until}` [Reopen]. Same
claimant at epoch+1 (round reset, reviewer and waiver cleared, fresh lease,
live run tokens revoked); it never closes the item. `400 validation|
evidence_unverified|bad_state`.

### `POST /v1/set_kind`
Router. `{key, kind:"question"|"operator_task"}` for an `open` `lane`; sets
`operator` true and the kind's default `wait`. `200 {key, kind, was:"lane"}`
[SetKind]. `400 validation|bad_state`.
