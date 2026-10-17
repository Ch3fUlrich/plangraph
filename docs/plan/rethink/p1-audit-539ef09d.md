# P1 audit (L3 qwen3.8-flash via qodercli, run 20261017-193107-p1-audit-qwen-e4c89b, main 539ef09d, rc 0). UNVERIFIED leaf output: each GAP is re-checked by the lane that fixes it (failing test first).
| clause | IMPL / GAP | TEST | note (≤15 words) |
|---|---|---|---|
| Runs card/attempt activities on Temporal | IMPL worker/workflow.rs:105; worker/activities.rs:248 | LIVE-ONLY: worker/tests/e2e.rs:431 | maximum_attempts=1; live run needs server |
| Worker scoped to assigned target only | GAP (worker/activities.rs:32) | NONE | shared queue "pg-cards"; claim omits contract `key` |
| Worktree per attempt from base sha | IMPL worker/attempt.rs:137 (run_attempt_inner); core/worktree.rs:53 | worker/attempt.rs:689 | worktree cleared+recreated each attempt |
| opencode run against OmniRoute model | IMPL worker/harness.rs:59 (`opencode run -m`) | NONE | no test exercises real opencode binary |
| Window-class↔leg combo enforced in worker | GAP (worker/workflow.rs:360) | NONE | UpgradeClass records class; never revalidates leg max |
| Checks + positive write-set confinement | IMPL worker/attempt.rs:216; core/confine.rs:50 | worker/attempt.rs:576 | empty write set allows nothing |
| Outcome probe before retry | IMPL worker/workflow.rs:253; worker/activities.rs:197 | worker/activities.rs:702 | probe once per non-ok exit |
| Heartbeat 30s / timeout 2min / stall 10min | IMPL core/config.rs:50-53 | core/config.rs:70 | heartbeat_timeout=120s is the "2min" |
| Exit classes | IMPL core/exit_class.rs:8 (15 classes) | core/budget/tests.rs:66 | classify priority exit_class.rs:115 |
| Retry budget 3 + one tier promotion | IMPL core/budget/mod.rs:163,:167 | core/budget/tests.rs:97,:123 | promotion once per card, ladder checked |
| Flaky detector | IMPL worker/attempt.rs:391 (check_with_rerun) | worker/attempt.rs:622 | flaky_check→Quarantine budget/mod.rs:128 |
| Usage-limit re-route, then capped jittered wait | IMPL core/budget/mod.rs:178; workflow.rs:364 | core/budget/tests.rs:169,:193,:219 | durable timer; wait_cap 24h |
| Merge under keeper advisory lock + fencing token | GAP (worker/mergelock.rs:44) | worker/mergelock.rs:337,:347 | local flock+CAS only; no keeper lock, no token |
| Confinement recheck at merge | GAP | NONE | mergelock.rs never rechecks write set |
| Rebase + re-check on base move | IMPL worker/workflow.rs:237 | worker/mergelock.rs:303 | rebase = fresh attempt on new head, re-runs check |
| Janitor | IMPL core/janitor.rs:59; worker/workflow.rs:537 | worker/activities.rs:792 | sweeps on every terminal path |
| Build ids + replay tests | IMPL worker/buildid.rs:15; bin/worker.rs:71 | worker/tests/replay.rs:38,:100 | offline replay; drift fails replay (good) |
| Harness versions pinned | GAP | NONE | temporalio/CLI pinned; opencode/qodercli not |

## RT-P1-PROJECTOR

| clause | IMPL / GAP | TEST | note (≤15 words) |
|---|---|---|---|
| Append-only events keyed (wf,run,activity,attempt) | IMPL pg/projector/apply.py:65 (ledger), :94 | t/test_projector_apply.py:76 | idempotent re-apply is a no-op |
| States move forward only | IMPL apply.py:144 (run_ord/event_id) | t/test_projector_apply.py:119,:129 | stale events counted, never written |
| Full rebuild from history tested | IMPL apply.py:214 (project_source) | t/test_projector_rebuild.py:99,:139 | rebuild==project; worker fixtures conformance |
| Rebuild against live Temporal | LIVE-ONLY | t/test_projector_live.py:130; test_projector_temporal_source.py | skips without server |
| API exposes watermark | IMPL pg/projector/cli.py:101; ddl.py:28 | t/test_projector_apply.py:213 | CLI subcommand + table only |
| UI exposes watermark | GAP | NONE | pg/web has zero watermark/projector references |
| Nightly reconciler raises Inbox alarm | IMPL pg/projector/reconcile.py:105; deploy/systemd/plangraph-projector-reconcile.timer:5 | t/test_projector_reconcile.py:165; t/test_inbox_alarms.py:90 | 03:30 timer; alarms surfaced via /inbox |
| Deadlines stored as values; keeper runs no timer | IMPL pg/keeper/schema.py:179 | t/contract/test_inbox.py:155 | past deadline changes nothing by itself |
| Timer lives only in Temporal | GAP | NONE | production workflow has no deadline timer; spike-only |

## RT-P1-REVIEW

| clause | IMPL / GAP | TEST | note (≤15 words) |
|---|---|---|---|
| Reviewer as Temporal activity | GAP | NONE | Python lib only (pg/review/seat.py:68); nothing in rust/ |
| 1 or 2 families by risk | IMPL pg/review/family.py:91 (pick_legs); pg/keeper/store_review.py:155-181 | t/test_review_seat.py:102; t/test_keeper_two_reviewers.py:57 | keeper enforces tier; no production caller maps tier→need |
| Full phase diff, split by module, no truncation | IMPL pg/review/prompt.py:87 (split_parts) | t/test_review_prompt.py:48,:60 | whole files ≤60k; oversize split by lines |
| Verdict schema pass/fix/halt | IMPL pg/review/verdict.py:25,:57 | t/test_review_verdict.py:190 | gaming gate verdict.py:166 |
| Fix creates fix card | GAP | NONE | verdict ok=False opens finding only (store_review.py:205) |
| Halt raises Inbox question | GAP | NONE | halt handled like fix; no ask_question call |
| Timeout reruns on another family | GAP (seat.py:12 detects) | t/test_review_seat.py:116 | down= caller-supplied; no automatic switch |
| Gate command as worker activity | GAP | t/test_planner_readiness.py:145 | readiness is planner Python (readiness.py:289); no gate activity |

## RT-P1-INBOX

| clause | IMPL / GAP | TEST | note (≤15 words) |
|---|---|---|---|
| Structured question API | IMPL pg/inbox/routes.py:178; pg/keeper/store_cards.py:270 | t/contract/test_inbox.py:56 | key/text/default/deadline/risk_tags validated |
| Attempt goes needs_input | GAP (worker/attempt.rs:230) | NONE | Signals::default never sets needs_input; planner gate only |
| Minimal answer form | IMPL pg/inbox/form.py:3 (PAGE) | t/test_inbox_form.py:6; t/contract/test_inbox.py:218 | self-contained, tight CSP |
| Answer emits Temporal signal | IMPL pg/inbox/temporal_signal.py:57 | t/test_inbox_signal.py:190,:274,:309 | signal after commit; failure never rolls back |
| Run resumes on answer | GAP | LIVE-ONLY send test t/test_inbox_signal.py:394 | workflow.rs:431 has only `cancel`; answer handler in spike only |
| Default recorded as question_timeout | IMPL store_cards.py:332-341; temporal_signal.py:94 | t/test_inbox_signal.py:245 | recording exists; nothing fires it automatically |
| Risk-tagged have no default | IMPL store_cards.py:285 (NO_DEFAULT_TAGS:38) | t/contract/test_inbox.py:79 | refused at ask and at timeout |

## RT-P1-E2E

| clause | IMPL / GAP | TEST | note (≤15 words) |
|---|---|---|---|
| Card passes readiness | IMPL pg/planner/readiness.py:289; pg/planner/service.py:101 | t/test_planner_readiness.py:145; t/test_planner_service.py:71 | hermetic gate + needs_input path planner-side |
| Routes to free leg | IMPL pg/planner/legs.py:54; pg/planner/routing.py:105 | t/test_planner_routing.py:288 | free tier first, qodercli-qwen head |
| Runs via Temporal + Rust worker | IMPL worker/workflow.rs; LIVE-ONLY | worker/tests/e2e.rs:431 | skips unless PG_TEMPORAL_ADDR |
| Keeper starts the workflow | GAP | NONE | no repo code spawns `card:<key>` workflow; test starts it itself |
| Check passes + merges; attempt visible | IMPL worker/workflow.rs:222; LIVE-ONLY | worker/tests/e2e.rs:431-458 | asserts main==sha, keeper.completed |
| Cost visible in keeper | GAP (apply.py:110 column exists) | NONE | Rust harness emits no tokens/cost — always NULL |
| Inbox answer resumes run | GAP | NONE | same missing answer signal as INBOX |
| No persistent orchestrator session | IMPL temporal_signal.py:1 (stdlib only); worker stateless | NONE | keeper never imports temporalio; projector pulls |

GAPS (non-LIVE): 15

1. **No `answer` signal / deadline timer in production card workflow** (INBOX resume, PROJECTOR timer, E2E resume). Fix: port spike card.rs:70-93 — add answer signal handler + timer-to-deadline in workflow.rs.
2. **Worker never produces needs_input** (attempt.rs:230 discards the flag). Fix: set Signals.needs_input in classify + map ExitClass::NeedsInput through to park-with-question.
3. **No keeper→Temporal start glue** — nothing spawns `card:<key>`. Fix: small spawner calling ConnectClient with the claimed LegSpec (Python or worker main).
4. **Cost/tokens never reported** — schema+projector ready, harness silent. Fix: parse opencode usage JSON into AttemptReport cost/tokens fields.
5. **Reviewer not a worker activity** — review is a library invoked externally. Fix: add a Rust activity shelling `tools/seat_verdict.py`-driven seat run on the phase diff.
6. **Gate command not a worker activity** — readiness lives in planner Python. Fix: activity wrapping `readiness.check_card` (subprocess) or document it as planner-side only.
7. **Fix verdict doesn't create a fix card**. Fix: in store_review.py `_verdict_changes`, insert a card with `fixes=key` (item_fixes.py already validates).
8. **Halt doesn't raise an Inbox question**. Fix: store_review halt branch calls existing `store.ask_question` with risk tag, no default.
9. **Timeout doesn't auto-reroute to another family**. Fix: run_seat retry loop adding failed family to `down=` when finish_reason=="timeout".
10. **Merge lock not keeper-advisory, no fencing token** (local flock only). Fix: keeper `/v1/merge_lock` endpoint handing token; mergelock.rs takes token before update-ref.
11. **No confinement recheck at merge**. Fix: run `confine::check_confinement` on the commit diff inside mergelock.rs before the CAS.
12. **No per-target worker scoping; claim body misses contract `key`**. Fix: per-target task queue name from config and send `key` in claim (keeper.rs:107).
13. **Window-class not revalidated on leg** — upgrade only records class. Fix: attempt start rejects class outside `LegSpec` windows; UpgradeClass reroute chooses a supporting leg.
14. **Harness versions unpinned** (opencode/qodercli; CI pins only Temporal CLI). Fix: CI step installing pinned opencode version + harness.rs startup version check.
15. **Watermark not exposed in UI** (docs claim it is). Fix: render projector_watermark on the existing status page (pg/web).

AUDIT-END
