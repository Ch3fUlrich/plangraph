# LE-NO-PROGRESS-ALARM-FOR-AN-IDLE-FLEET

Outcome. keeper alerts lists no_progress while no item reached done for no_progress_s and ready p10 work is unclaimed, and a feeder host whose feeder.off is older than the policy limit while a writer slot is free sends one routing alert naming the host.

- plan_source. Keeper item plangraph:LE-NO-PROGRESS-ALARM-FOR-AN-IDLE-FLEET, operator 2026-11-27 10:02Z (D-7041, rule 3.110), priority 10, D-7042, D-7047.
- base commit. 443bf493af86a2ccd1d2859fb4e1d52eceea522d
- card SHA-256. 766125f0f9c869bbd0dfb6a2f65bece9abf575e2caa835ef5374d4665bc2079f
- tree SHA-256. 374f5d3917e239e9eb4de45b4bb8fce2103c723b363904fc3d2e0183cb6f3f29
- evidence_after_deploy. After the keeper restart: `pg alerts` on coding.vm during a staged window (no done item for more than `no_progress_s`, at least one ready p10 item) shows the `no_progress` line, and the first `pg alerts` after the next done item does not. After the feeder binary and policy reach one host (rule 3.112: one host first): touch `feeder.off` with an mtime older than the limit on that host and record the routing message with rule `feeder-off-idle` and its `event_id`; remove the file. Record both in the item evidence; then retire plan-roadmap's `~/.cache/pr0961/idlecheck.py` stopgap.

## Checked against base

- `_alerts` returns `lease_expiring`, `review_overdue`, `findings`, `refusals`, `history`, `stalled`, `warnings`; nothing looks at the time of the last done item. `stalled` needs no ready item, so 122 open p10 items never trip it. [src/plangraph/keeper/store_read.py:298-330]
- An item reaches `done` through three event types: `review_passed` (the review pass and the automatic merge close), `closed_on_decision` (the router's decision close) and `answered`; `_run_derived` already reads exactly these as the done time. [src/plangraph/keeper/events.py:262-273; src/plangraph/keeper/store_read.py:117-132; src/plangraph/keeper/store_review.py:400-415]
- `views.ready(..., agent=True)` drops operator flags and operator kinds, the selection the feeder reads (`ready?...&agent=1`); gates and goals stay in it. [src/plangraph/views.py:274-301; rust/plangraph-cli/src/feeder/runtime.rs:68-70]
- Alert timings are keeper config integers applied at daemon start (`review_overdue_s`, `lease_warn_s`, `alert_window_s`). [src/plangraph/keeper/config.py:127-136; src/plangraph/keeper/daemon.py:75-87; src/plangraph/keeper/store.py:63,114]
- The keeper holds no feeder state: `feeder.off` is read only on the host by the feeder tick as `HostFacts.kill_switch`, and `admit` refuses `kill_switch` first, as a healthy exit 0. [rust/plangraph-cli/src/feeder/sources.rs:221-229,288; rust/plangraph-cli/src/feeder/mod.rs:145-160; rust/plangraph-cli/src/feeder/runtime.rs:122-196,276-288]
- Host problems already reach routing through the host-link outbox (`<state>/watchdog/outbox`) and the bridge, which posts a keeper message with a stable `msg_id`; the keeper deduplicates a repeated `msg_id` with the same body. [rust/worker-core/src/host_link.rs:56-128; rust/plangraph-cli/src/host_link_bridge.rs:76-148; src/plangraph/keeper/store_messages.py:167-190; docs/host-link.md]

## Decisions

- D1. Keeper half. `_alerts` adds `no_progress`: `null`, or `{"idle_minutes", "last_done", "ready_p10", "claimed"}` when both hold in the requested scope: (a) the newest completion event (`review_passed`, `closed_on_decision` or `answered`) of a scope item is older than `no_progress_s`, or, when the scope has no completion event, the oldest `item_created` event of the scope is older than `no_progress_s`; (b) at least one item of `views.ready(items, project=project, agent=True)` has `kind == "lane"` and priority 10 (work an agent writer can take; operator steps, gates and goals do not count). `idle_minutes` counts from `last_done`, or from that oldest creation time when there is no completion (`last_done` is then `null`); `claimed` counts scope items in `claimed`. It is computed on every read, so it clears with the next done item; no table, no finding row.
- D2. `no_progress_s` is keeper config (JSON integer 1..315360000, like the other alert timings), default 3600, store default `NO_PROGRESS_S = 3600`. EXPERIMENT (rule 3.76): the missed window was 275-303 min; one hour bounds that loss to about a fifth. Measure: count of `no_progress` reads per day against items done per day for 7 days after deploy; lower or raise the default in config.
- D3. Text form: `no_progress idle <idle_minutes> min, <ready_p10> ready p10 unclaimed, <claimed> claimed, last done <last_done or ->`.
- D4. Feeder half. Host facts stay on the host. In `decide`, after the live writer budget is applied (`capacity::apply`, so the free-slot test sees the budgeted facts) and before the `stale_quota` / `no_eligible_item` refusal returns, call `off_alert::check(home, env, state, &facts, &policy, eligible, o.dry)`. It returns one of `not_off`, `not_due`, `no_free_slot`, `no_eligible_item`, `no_state_root`, `queued`, `already_queued`, `queue_failed`, which the tick row carries as `feeder_off_alert`. The check is due when `facts.kill_switch` is true, the mtime of `~/.config/plangraph/feeder.off` is at least `policy.feeder_off_alert_seconds` old, `eligible` is true, and `admit(&HostFacts { kill_switch: false, ..facts.clone() }, &policy) == Admission::Admit` (a slot is free).
- D5. `Policy.feeder_off_alert_seconds: i64`, default 21600; `deploy/feeder/policy.toml` sets `feeder_off_alert_seconds = 21600`; a negative value is `invalid_policy` like the other ages. EXPERIMENT: the 11-25..11-27 stop lasted 54 h; 6 h passes a planned maintenance stop and catches a forgotten one within one shift. Measure the alerts per week.
- D6. Delivery reuses host-link. `pg_worker_core::host_link::enqueue_feeder_off_idle(root, host, since)` writes `<root>/watchdog/outbox/<since:020>-feeder-off-idle-<id>.json` with `kind: "alert"`, `rule: "feeder-off-idle"`, `recipient: "routing"`, `item_key: "plangraph:LE-NO-PROGRESS-ALARM-FOR-AN-IDLE-FLEET"`, `host`, `down_since: since`, `event_id: sha256("feeder-off-idle:<host>:<since>")`. `root` is env `PLANGRAPH_SPAWN_STATE` (the host-link state root); unset or empty gives `no_state_root`. The feeder keeps `<feeder state>/feeder-off-alerted` holding the last queued `since`; equal `since` gives `already_queued`, so one episode (one `feeder.off` mtime) is queued once. The marker is written only after the outbox write succeeded; an outbox write error gives `queue_failed` (the error text goes to the row's `feeder_off_alert_error`), leaves the marker unchanged, and the next tick tries again. `o.dry` never writes and reports what it would do.
- D7. The bridge accepts rule `feeder-off-idle` and posts the text `feeder.off on <host> since <since> while a writer slot is free and ready work exists`. The text has no age, so a re-delivery has the same body and the keeper keeps one message.
- D8. The "free slots but no writer, feeder on" case is covered by D1 (ready p10 unclaimed and nothing done); no second host check.

## Entry points

- E1. `Store.alerts` (`GET /v1/alerts`, `pg alerts`, JSON and text).
- E2. One feeder tick as deployed (`plangraph feed --once --live-facts`, `deploy/systemd/pg-feeder.service`) and the host-link bridge (`host-link-deliver`).

## Acceptance

| Row | Entry | Observable result | Evidence |
| --- | --- | --- | --- |
| B1 | E1 | Last done item 61 min ago, `no_progress_s=3600`, one ready unclaimed p10 item, two claimed: `no_progress` is `{"idle_minutes": 61, "ready_p10": 1, "claimed": 2, ...}` and the text line names 61 and 1. | X1 |
| B2 | E1 | The same store with a done item 59 min ago, or with no ready p10 item (only p9 ready), reports `no_progress: null`; a `project` filter counts only that project. | X1 |
| B3 | E1 | Config `no_progress_s` is read and validated like `review_overdue_s` and applied to the daemon store. | X2 |
| B4 | E2 | `feeder.off` 7 h old, slot free, eligible item, state root set: one outbox file with rule `feeder-off-idle` naming the host; the next tick returns `already_queued` and writes nothing. 5 h old: `not_due`. `running_writers` at the host limit: `no_free_slot`. | X3 |
| B5 | E2 | The bridge posts one keeper message to routing whose text names the host and `since`, with `msg_id` = `event_id`. | X4 |

## Verification

Write X1-X4 first; on the base X1 finds no `no_progress` key, X2 rejects the unknown config key, X3/X4 do not compile (`off_alert`, `enqueue_feeder_off_idle`, rule unknown).

- X1. `tests/test_keeper_no_progress_alert.py::test_no_progress_names_idle_minutes_and_ready_p10` and `::test_no_progress_clears_after_a_done_item_and_ignores_lower_priorities` (World with the injected clock, `defn(..., priority=10)`, close one item through the normal review pass, `w.clock.advance`), `::test_a_decision_close_or_answer_counts_as_progress` (the alert stays off after `close_on_decision` and after an answer), `::test_a_store_without_completions_uses_the_oldest_creation` (no alert before `no_progress_s` from the first item, alert after) and `::test_operator_only_ready_work_does_not_alert` (only a p10 `operator_task` and a p10 goal ready).
- X2. `tests/test_keeper_no_progress_alert.py::test_no_progress_s_is_validated_config` (pattern of `tests/test_keeper_lease_cfg.py`).
- X3. Unit tests in `rust/plangraph-cli/src/feeder/off_alert.rs`: `queues_once_for_an_old_feeder_off_with_a_free_slot`, `young_file_full_host_or_missing_state_root_queue_nothing`, `dry_run_writes_nothing`, `a_failed_outbox_write_reports_queue_failed_and_retries_next_tick` (make the outbox directory unwritable, then writable). Live path: `rust/plangraph-cli/src/feeder/runtime/live_tests.rs::feeder_off_alert_reads_the_budgeted_capacity` (a `decide` run whose raw facts have a free slot but whose writer budget has none reports `no_free_slot`). Use a temp home and set the mtime with `std::fs::File::set_modified` (no new dependency).
- X4. `rust/plangraph-cli/tests/host_link_bridge.rs::feeder_off_idle_reaches_keeper_once` (pattern of `disk_hygiene_zero_progress_reaches_keeper`) and a unit test `feeder_off_idle_event_has_a_stable_id` in `rust/worker-core/src/host_link.rs`.
- X5. Held out: the card's `heldout_check` (lease config, API, feeder integration tests) stays green.

## Scope and reuse

- R1. Reuse `views.ready(agent=True)`, the three completion event types, keeper config validation, `feeder::admit`, `host_link::write_outbox`, the bridge's keeper delivery and the keeper's `msg_id` dedupe.
- N1. No new keeper table, verb or host ingest; no change to `stalled`, to snapshot-only local alerts (`cli_local.py`) or to the feeder's admission order. Enabling on hosts follows rule 3.112 (one host first) and is a live step.

### File budgets

Per-file caps are in the card `line_caps` and the TSV. plangraph:LE-WS-BUILD-SSH-KEX-RESET-BLOCKS-ORCHESTRATION writes `host_link.rs`, `host_link_bridge.rs`, its test and `docs/host-link.md`, and two keeper items write `docs/KEEPER.md`; the keeper's `write_overlap` gate runs them one after another, and the final caps of the shared files hold in either order (`host_link.rs`: 329 + 45 + 90 = 464). `off_alert.rs` is a new module (registered in `feeder/mod.rs`) so `runtime.rs` grows by the one call and the row field only.

### Implementation

- store_read.py. In `_alerts`, read `SELECT key, MAX(ts) FROM events WHERE type IN ('review_passed','closed_on_decision','answered') GROUP BY key` and `SELECT MIN(ts) ... WHERE type='item_created'` per scope once, reduce to the scope keys, and build D1. Add `"no_progress"` to the reply dict.
- store.py / config.py / daemon.py. `NO_PROGRESS_S = 3600`; `self.no_progress_s = NO_PROGRESS_S`; `self.no_progress_s = self._positive_seconds(data, "no_progress_s", 3600)`; `store.no_progress_s = cfg.no_progress_s`.
- fmt.py. `alerts_text` prints the D3 line when `no_progress` is set, before `stalled`.
- feeder/mod.rs. `mod off_alert;`, the `Policy` field and default, and the negative-value check in `admit`'s policy guard.
- feeder/off_alert.rs. `pub(super) fn check(...) -> &'static str` per D4/D6.
- feeder/runtime.rs. Call `off_alert::check` per D4 and set `row["feeder_off_alert"]` on every returned row of `decide` after that point.
- worker-core host_link.rs. `pub const FEEDER_OFF_IDLE: &str = "feeder-off-idle";` and `pub fn enqueue_feeder_off_idle` per D6.
- host_link_bridge.rs. Add `FEEDER_OFF_IDLE` to the accepted rules in `run` and the D7 text in `deliver`.
- deploy/feeder/policy.toml, docs/writer-feeder.md (the `feeder_off_alert` row field and policy key), docs/host-link.md (the rule), docs/KEEPER.md (`no_progress` in `alerts` and the `no_progress_s` config key), changelog one line.

## Open points for design approval

None.

## Open review points for the implementer (round 2, final under rule 3.119, D-7092)

The spec is final after two design review rounds (gpt-6.2-sol-high). The points below were still open; the implementer resolves them inside the write set or reports why not.

VERDICT: DESIGN-CHANGE
CITATIONS: checked 16, wrong 1. The round 1 completion-event citation is fixed.
WRONG :15 rust/plangraph-cli/src/feeder/runtime.rs:68–70 shows the API call. `src/plangraph/keeper/store_context.py:445–523` shows the additional claim-readiness and reconciliation checks that determine the returned `items`.
FIT/TREE: Covers Keeper and feeder alerts. Staged delivery and stopgap retirement remain rollout steps. Eighteen write paths and both digests match.
POINTS: R1 D1 completion and E2 fixed at :22,34,50. All three completion events and the empty-history threshold are covered, and the command matches the unit.
POINTS: R1 D1/D8 still open, :22,29, src/plangraph/keeper/store_context.py:445–523. Bare `views.ready` still counts a p10 lane the production ready endpoint puts in `needs_reconcile` or `in_progress`. Reuse the agent-ready `items` selection and add an already-merged or open-PR case. BLOCKS
POINTS: R1 D4 placement fixed, but X3 is still open, :52 and card:109, rust/plangraph-cli/src/feeder/runtime.rs:274–275. The `feeder::off_alert` filter excludes `feeder::runtime::live_tests`. Add the live `decide` test to `check_cmd`. BLOCKS
POINTS: R1 D6 still open, :27,71–72, rust/worker-core/src/host_link.rs:56–60. Retry is specified, but the implementation’s `&'static str` return cannot carry `feeder_off_alert_error`. Return status plus error detail and assert both in the retry test. BLOCKS
POINTS: D10, :71 and .docs-map:30. If the docs ratchet lands first, the new `off_alert.rs` is unmapped. Add its mapping to `docs/writer-feeder.md` and list `.docs-map` in the card and tree. BLOCKS
