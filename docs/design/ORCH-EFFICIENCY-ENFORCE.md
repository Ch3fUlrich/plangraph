# ORCH-EFFICIENCY-ENFORCE design (operator D-2835, routing DECISIONS 3.13 and 0.2a)

Author: claude-opus-5-5 (strong-model design, DECISIONS 0.2a). Item: `plangraph:ORCH-EFFICIENCY-ENFORCE` (prio 0, review tier high).
Goal: R1 (orchestrators make small edits themselves), R2 (the spawner's `route` code decides tool and model, direct edit included) and R3 (code kills stalled runs) become code, so no prose rule has to be remembered.

Evidence (item text):
- run-ae0dcb3c wrote nothing after 17:17Z and used 17 s of CPU in 58 min; it held its slot until a hand cancel at 18:04Z.
- At 18:10Z other projects held all 4 coding.vm slots.
- CI-SELECT-DEPLOY-REFS was merged but could not be closed: the parser read "fix - LOW only:" as FAIL, a refuted MED could not be posted, and DECISIONS 3.4 says a refuted seat counts.
- ci_commit_gate refused hand edit 6ef7b09d (door path, and 12 > 10 lines).

Overlap with `PG-EFFICIENCY-GATES` slice 4 (same lane family; that design is in `docs/design/PG-EFFICIENCY-GATES.md`):
- Done criterion (3) is that design's slice 4b (`#5`), built on 4a's `cancel(job, liveness=)`.
- Done criterion (4) extends that design's host admission row, which moves here as slice E4 (4c keeps the scope and OOM rows).
- This document does not repeat those rows. It states only what changes and the order.

## Done criteria -> slices

| crit | rule | slice |
|---|---|---|
| (1) | `route` picks `direct_edit` or a writer leg by the cost + L*time model (D-2853, spec `status/direct-edit-cost-model.md` @ d97b5fb3); hard guards only: door/secret paths, context fill <= 70%, Claude quota floor, slice ceiling 400 | E3a + E3c (after E10a, E3b) |
| (2) | `ci_commit_gate` allows a `Hand-Edit` up to the slice ceiling (400 lines per commit and per Plan-Item in the range) outside `AUTH_DOOR_PATHS` and secret paths; above 50 lines it needs a `Route-Decision:` trailer || E1a + E1b |
| (3) | the spawner kills a writer run with no worktree write and < 2% CPU for 15 min (exit class `stall`, liveness `stalled`, slot freed, recorded) | E2 = PG-EFFICIENCY-GATES 4b, amended below |
| (4) | the host gate keeps at least 1 slot for every project with queued work; seats are counted apart from writers | E4a + E4b |
| (5) | the seat parsers accept the "LOW only" variants and post a seat that has a D-id refutation file as pass-with-lows (3.4) | E5 |
| (6) | one compiled boot file per level, superseded rules dropped, size cap in CI; a relaunched session reads only it + its card (D-2840) | E6 |
| (7) | runs outlive sessions: the spawner owns live runs and completion events per owner/claim; `spawner watch --owner X` re-attaches and replays pending completions | E7a + E7b |
| (8) | one live-state command (runs, PRs, CI, claims), so handoffs hold only decisions + next steps | E8 |
| (9) | claim heartbeats from the spawner while a run lives, no per-session shell loops | E9 |
| (10) | tokens per merged change + claim-to-merge time per item, in the router digest; token counts per run first, because the route model needs them (D-2853) | E10a + E10 |

## Slices

| slice | scope | max_lines |
|---|---|---|
| 1 | E1a gate: hand-edit ceiling 400, `Route-Decision:` format above 50 lines per item, secret/malformed paths, `doors.py` + drift test (crit 2) | 400(fix rounds exempt per D-2865) |
| 2 | E2 = PG-EFFICIENCY-GATES slice 4b with the writer/seat stall split below (crit 3); Plan-Slice stays `PG-EFFICIENCY-GATES.md#5`, this row only books the order | 400 |
| 3 | E3a pure route core: guards, `route_stats`, prices, cost model, `RouteDecision` (crit 1, part 1) | 400 |
| 4 | E4a host admission (row moved from PG-EFFICIENCY-GATES 4c) + host registry + lock (crit 4, part 1) | 400 |
| 5 | E5 seat parsers: one extractor, LOW-only variants, router refutation held by the keeper (crit 5) | 400 |
| 6 | E6 compiled boot file per level + CI size cap + relaunch prompt, incl. the `levels` rule field in `store_rules._rule` and `decisions_view` (crit 6) | 400 |
| 7 | E7a detached run supervisor + start handshake + supervisor reaper (crit 7, part 1) | 400 |
| 8 | E8 live-state command (crit 8) | 300 |
| 9 | E9 claim heartbeat from the run supervisor (crit 9) | 250 |
| 10 | E10 tokens per merged change + claim-to-merge metrics (crit 10) | 300 |
| 11 | E4b writer/seat slots, queue-first admission, cross-project order, queue sweeper (crit 4, part 2) | 400 |
| 12 | E11 wake budget: `watch --block --batch` with decision-only wake classes and one digest | 250 |
| 15 | E3b seat-family selection from the PR's writer families (incl. Hand-Edit = orchestrator family) + `seat_options` for E3's seat-loss term (D-2855) | 250 |
| 14 | E10a token counts per run in the record, `result.json` and the keeper run post, usage parsers on recorded fixtures (crit 10, part 1; input to E3a) | 300 |
| 16 | E1b `lane_gates.py hand-edits`: Route-Decision ids checked against keeper route entries (crit 2, part 2; needs E3c) | 200 |
| 17 | E3c route wiring: route record + keeper `route` entry, plan fields, `--ctx-*`/`--orch-model` flags, spawn refusal (crit 1, part 2) | 250 |
| 13 | E7b per-owner completion inbox, one run identity key, claim epoch on events, `watch --owner` (crit 7, part 2) | 300 |

Build order with slice 4 (one task loop, routing D-2837, D-2840):

4a (written, `a43a7ff8`) -> E1a -> E2 (4b), with E5 in parallel (D-2844: no dependency, no cost) -> 4a3 -> E7a -> E7b -> E9 -> E11 -> E10a -> E3b -> E3a -> E3c -> E1b -> 4a2 -> E4a -> E4b -> 4c (scope and OOM) -> E6 -> E8 -> E10 -> phase review. The operator may reorder the rest (Q-INV-1, routing report 2026-10-25-throughput-cost-investigation §6); until then this order holds (D-2844).

- 4a3 moves ahead of E7a (seat r2 G5): E7's owner fallback is the 4a3 principal name. E7a carries its own user-manager probe (`systemctl --user is-system-running`, injected), so it does not wait for 4c; 4c later reuses that probe for the scope launch.
- E7 and E9 follow E2 because they share the run-liveness code (the supervisor that polls the run also heartbeats the claim and writes the completion), D-2840.

- E1 and E2 land first, as the item asks.
- E2 and E4 are the same rows as PG-EFFICIENCY-GATES 4b and 4c's host admission (D-2837): their commits carry two Plan-Slice trailers, `docs/design/PG-EFFICIENCY-GATES.md#5` (resp. `#6`) and `docs/design/ORCH-EFFICIENCY-ENFORCE.md#2` (resp. `#4` for E4a), so both items close on the same evidence.
- E1 touches the gate path `tools/ci_commit_gate.py`, so it is a spawned writer with `Gate-Edit: D-2835`, never a Hand-Edit. It is cut after build-L1's interim Gate-Edit 10 -> 50 (D-2853) when that merges first, and rebased on it otherwise.
- E10a comes before E3 (D-2853): the route model's writer cost uses measured tokens per run, not only catalog figures.
- E2 needs only 4a: without 4a2, a stalled run ends `failed` with no fall-through, which still meets crit (3).
- E5 has no dependency. It may run in parallel once lane/review-verdict-parse (the build-L1 mimo-pro writer for the 6ef7b09d spec) has merged, because E5 builds on its parser.

### E1 hand-edit cap (crit 2)

| piece | where | behaviour |
|---|---|---|
| ceiling | `tools/ci_commit_gate.py` `HAND_EDIT_MAX_LINES = 400` (the slice cap, D-2739; a safety ceiling, not an efficiency rule, D-2853) | added + deleted lines per Hand-Edit commit, measured as today (`_changed_lines`, numstat); binary files are refused as today; the malformed-trailer check is unchanged. Range ceiling (delta2 seat H12, D-2852 with ceiling 400 per D-2853): the gate also sums the lines of all Hand-Edit commits in the checked range (`BASE..HEAD`) per `Plan-Item:` value and refuses a bucket over 400 (`hand-edit total over ceiling for <item>`). Every Hand-Edit commit must carry exactly one `Plan-Item:` trailer, else `hand-edit without Plan-Item` (delta4 seat M10: a trailer-less Hand-Edit would join no route-decision sum); item keys compare case-folded (E1a seat G4) |
| route decision (E1a format, E1b record) | `ROUTE_DECISION_LINES = 50`, `ROUTE_DECISION_RE = ^Route-Decision: route-[0-9a-f]{32}$` | per Plan-Item over the whole history, not only the range (delta3 seat K5/K6): the gate sums the changed lines of every Hand-Edit commit reachable from HEAD that carries the same `Plan-Item:` (CI checks out with full history; merge commits are checked like any other commit, measured by their combined diff (`git diff-tree --cc`, only the conflict-resolution lines; a clean merge counts 0), and a squash commit carries the squashed bodies, so its trailers still count); every Hand-Edit commit at which that running sum exceeds 50, and every later one of the same item, must carry exactly one well-formed `Route-Decision:` trailer, else `hand-edit over 50 lines for <item> without Route-Decision`; two trailers are refused too. Commits before the range only feed the running sum; violations are reported only for commits in the range. So 400 lines spread over eight sequential 50-line PRs still need trailers from the second PR on. Parallel open PRs of one item each see only their own history (delta4 seat M10): the merger re-runs `ci_commit_gate` with BASE = current main and HEAD = the PR head right before merging (merge checklist, next to `gate_wiring_check`), so the second PR to merge fails there. The 400 ceiling stays per commit and per item within the range. Until E3c lands no route id exists, so in that window an item's Hand-Edits are effectively capped at 50 lines in total, which equals build-L1's interim Gate-Edit (D-2853). Record check (E1b): CI cannot reach the keeper, so `tools/lane_gates.py hand-edits --range BASE..HEAD` checks each id against the keeper's `route` log entry for the item (E3c posts it at decision time, append-only, principal- and time-stamped, so it cannot be back-filled unseen): the entry exists, was logged before the commit was first pushed (`committer date` is not trusted; the anchor is the earliest CI run, read from `gh`, whose head sha is that commit or a descendant of it in the PR, so later commits are judged against their own first push, delta4 seat M12), says `mode=direct_edit`, its `est_lines` lies within 0.5x..2x of the commit's changed lines (a band, not a floor, M13), and its route id is cited by no other commit reachable from HEAD or in the range (one decision, one commit, M13). The check fails closed: keeper or `gh` unreachable, or an unparseable answer, is a red gate, never green (M14). Enforcement point (M14): the merger runs `lane_gates.py hand-edits` in its merge checklist like `gate_wiring_check` and does not merge on red; main takes changes only through PRs. Not covered: an L1 that posts a false route entry before its own commit; the entry is principal-stamped and audited, so this is visible, not prevented |
| secret paths in the gate (seat r1 E4) | `tools/ci_commit_gate.py` gains `SECRET_NAME_PATTERNS`, `SECRET_DIRS` (copied from `gates/privacy.py`) and `_secret_path(path)` with the same logic as `privacy.secret_path` | a Hand-Edit touching a secret-shaped path is refused (`hand-edit touches secret-shaped path`), next to the existing door refusal, so IE1 holds at the gate, not only in route |
| shared lists | new `src/plangraph/gates/doors.py`: `AUTH_DOOR_PATHS` (same tuple), `HAND_EDIT_MAX_LINES = 400`, `ROUTE_DECISION_LINES = 50`, `canon_path(path)` (backslash -> `/`, leading `./` dropped, `posixpath.normpath`; absolute paths and any `..` segment -> `None`, which callers treat as blocked), `door_path(path)` (fnmatchcase over the tuple, on the canonical path), `blocked_path(path) -> "door" or "secret" or "malformed" or None` (delta2 seat H9: `./src/plangraph/keeper/x.py` and `src\plangraph\keeper\x.py` are doors). The gate copies `canon_path` too | `ci_commit_gate.py` stays self-contained, because CI runs the BASE copy from `$RUNNER_TEMP` (`.github/workflows/tests.yml`, `git show $BASE:tools/ci_commit_gate.py`) and cannot import `src`. Drift test (seat r1 E11): the tuples, caps and secret lists are equal, AND a shared fixture of about 40 rows `(path, gate_verdict, route_verdict)` pins the expected answer per path, not only agreement (delta2 seat H17): doors, secrets, `./` and backslash forms, gate-blocked paths such as `src/plangraph/authoring.md` (matches `src/plangraph/*auth*`, so `door`), gate-pass paths that the old route-only patterns blocked such as `docs/oauth.md` and `src/plangraph/author_list.py`, token-named paths such as `docs/tokens.md` and `tools/token_count.py` (gate_verdict `secret`: `privacy.SECRET_NAME_PATTERNS` is copied unchanged, so its `*token*` row stays and over-matching refuses, the safe side; E1a seats G2), and true near misses such as `docs/keys.md`; the gate's own loop and `doors.blocked_path` must both give `gate_verdict`, and E3's guard step must give `route_verdict` (`writer` exactly when `gate_verdict` is not None: D-2853 keeps door and secret paths as the only path guards, so the old route-only `*auth*`/`*token*` name patterns are dropped; the privacy matcher, `*token*` included, is not touched) |
| bootstrap | `Gate-Edit: D-2835` (`GATE_PATHS` includes `tools/ci_commit_gate.py`) | the BASE copy judges the commit that raises the cap, so that commit is a writer commit (Writer trailer), never a Hand-Edit; the new cap and secret check bind only the commits after the merge. A PR that adds a door to both copies is judged by the old BASE list for its own commits; that is the accepted bootstrap window, and the drift test catches a door added to only one copy |

E1 tests:
- Hand-Edits of 400 lines with a Route-Decision (pass), 401 (refused), 30 lines touching `tools/seat_verdict.py` (door, refused);
- 50 lines without Route-Decision (pass), 51 without (refused), 51 with a malformed one or with two (refused), 51 with one well-formed (pass);
- an item with an earlier 40-line Hand-Edit in history: a new 20-line one without Route-Decision (refused), with one (pass); a later 5-line one of the same item without (refused);
- a Hand-Edit without `Plan-Item:` (refused); two Hand-Edits of one item whose keys differ only in case share one bucket; a merge commit carrying a 60-line Hand-Edit without Route-Decision (refused);
- (E1b) `lane_gates.py hand-edits` with a fake keeper and fake `gh`: a missing route entry, an entry logged after the first CI run of the citing commit (flagged) and one logged after the PR's first CI run but before that commit's first run (pass), `mode=writer`, another key, `est_lines` outside 0.5x..2x, one id cited by two commits, and keeper or `gh` unreachable (red) each checked;
- Hand-Edits touching `vault/x.json` and `config/.env` (secret, refused);
- `./src/plangraph/keeper/x.py`, a backslash door path and a `..` path (refused);
- two 210-line Hand-Edits for one Plan-Item in one range (refused), and for two items (pass);
- the drift test with the shared fixture;
- the existing gate suite stays green.

### E2 stall kill = 4b, amended (crit 3, writer runs)

4b ships as designed: polling monitor, injected `sleep`/`clock`/`tick`, pure `liveness.check`, and the waiting exemption. The kill is `runner.cancel(job, liveness="stalled")`, which ends the run with `exit_class=stall`, frees the process and its slot (no runner job left `running`), and records `liveness`/`exit_class` in the record and in `result.json` (4a). Crit (3) is read as "a writer run" (seat r1 E15). The amendments:

| role class (4a `spawn_limits` map) | stalled when, for `stall_min` (15) | why |
|---|---|---|
| writer (`implement`, `fix`, `writer`) | no worktree work (below) AND process-group CPU < `stall_cpu_pct` (2) AND no work child (delta2 seat H6: a descendant counts as a work child only when it is not a zombie, its executable basename is not in `spawn_limits.helpers[harness]` (the harness's own MCP servers, LSPs and watchers; default lists per harness from the measured trees, e.g. opencode's serena/graphify/context7/omnigraph/playwright), and it used CPU time during the window (> 0 ticks); a detached worker that left the process group is not seen and does not keep the run alive) | crit (3) as written. New stdout bytes do not count as work for writers: run-ae0dcb3c streamed nothing useful for 58 min. A running test child keeps the run alive (seat r1 E8: an I/O-bound or sleeping suite has low CPU and no writes); a hung test is then bounded by the wall cap (4a2), not by the stall rule |
| worktree work | a commit on the run branch whose tree differs from its parent's (an empty commit is not work, seat r1 E9), or a file under cwd (outside `.git/` and `limits.ignore[harness]`) whose content hash has not been seen before for that path in this run | hashes are kept in one run-wide set (not per path, delta2 seat H5), up to 4096 entries, oldest evicted first: content already seen anywhere in the run is not work, so a loop that cycles contents over one path, round-robins them over many paths or copies them into fresh files is stalled; a path revised many times keeps its signal as long as each content is new. Accepted gap: a loop that writes ever-new content (e.g. a counter) looks like work and is bounded by the wall cap (4a2), not by the stall rule (replaces 4b's "last 8 hashes", seat r1 E9) |
| probe failure | `liveness.check` | an unreadable CPU, child list or worktree (no `/proc`, permission error, probe raises) reads as alive, never as 0% or none (delta2 seat H7, same direction as E4a's registry rule): the run is then bounded by the wall cap only, and the record gets `liveness_probe: unavailable` once |
| seat (`review`) | no new stdout/stderr bytes AND CPU < 2% | a seat writes no files. Whether qodercli `-p`, `opencode run` and agy `-p` stream output before they end is not verified (F1). The seat rule ships only for harnesses in `spawn_limits.streams` (default empty); every other seat is bounded by its 45 min wall cap. E2's tests are hermetic and cannot verify F1 (delta2 seat H8), so a harness is added to `streams` only by a later config change whose PR records one real seat run of that harness showing stdout bytes before exit (D-2839: that run is the first real seat after E2 merges, not a separate probe run). E2 itself merges with `streams` empty |
| default | as writer | |

- Per-leg window (routing D-2843; build-L1-bus saw mimo-pro work in single turns of 4-20 min, and run-c3b5a522 and run-8cba759a were cancelled under a flat 15 min rule, at least one mid-turn). `spawn_limits.stall_min_by_leg` (map leg id -> minutes) overrides `stall_min` per leg. The default is set from F5: `mimo-v2.6-pro` 45, `mimo-v2.6-flash` 20, `opencode` free legs 20, and every leg without measurements 45. A leg's window is never below its measured max turn gap, rounded up to 5 min.
- Harness stream bytes (D-2843 ask) were checked on coding.vm, 2026-10-25 19:07-19:16Z, with live mimo-pro runs (wt4a, wtfix, seatenf2/3):
  - opencode 2.1.0 persists a session's messages (`opencode.db` `session_v2.time_updated`, `session_message`) only when a message ends: wt4a showed no change for 490 s mid-turn.
  - The shared `log/opencode.log` has no per-token or per-chunk lines.
  - The process tree's `/proc/<pid>/io` rchar grows about 180 B/s from MCP and watcher polling in every state, so it cannot separate a live stream from a dead one.

  No local per-run stream signal exists for opencode today, so the per-leg window is the mitigation. If a later opencode version persists partial parts, a probe on `session_v2.time_updated` for the run's `directory` can be added as a work signal for that harness.
- A wait inside the harness (its own 429 backoff) is invisible to the spawner. After 15 min with no work, CPU or child, such a run is stalled and, once 4a2 lands, falls through to the next leg. That cost is accepted; a harness that prints its waits can be added to the waiting exemption later.
- `spawn_limits` gains `streams` (a list of harness names; validation refuses any other shape).
- The 4b budget becomes 400 here and in PG-EFFICIENCY-GATES `#5`, because the role split, the child signal and the hash change add about 80 lines (seat r1 E14).

E2 tests add to 4b's:
- a writer streaming output with no writes, 0.5% CPU, 15 min -> stalled;
- a writer with a live pytest child, no writes, 0.5% CPU -> alive;
- an empty-commit loop -> stalled;
- a 12-content cycle on one file, the same contents round-robin over 5 paths, and copies into fresh files -> stalled;
- a file revised 300 times with new content each time -> alive;
- an idle MCP helper child and a zombie child -> no work child; a test child using CPU -> work child;
- an unreadable CPU probe, child list or worktree -> alive, `liveness_probe: unavailable`;
- a seat on a non-streaming harness, silent for 40 min -> alive until the cap;
- a seat on a harness in `streams`, silent with low CPU -> stalled;
- after a kill, the runner holds no running job and `result.json` has `exit_class=stall`, `liveness=stalled`.

### E3 route `direct_edit` by cost model (crit 1, R2; D-2853)

The operator replaced the line cap with a calculation (D-2853; spec `status/direct-edit-cost-model.md` at plangraph d97b5fb3, build-L1). `route` compares the orchestrator doing the change itself against delegating it, both in dollars plus latency.

| piece | where | behaviour |
|---|---|---|
| guards | pure `gates.direct_edit.guard(card, role, limits) -> reason or None`, first step of `decide` | `writer` with the failed guard as reason when any holds: `role != "implement"`; `card.est_lines` missing, not `routing._is_int` (bool excluded) or outside 1..`doors.HAND_EDIT_MAX_LINES` (400); the scope list (`card.paths` + `card.write_set`) empty, or any entry whose `doors.canon_path` is `None` or whose `doors.blocked_path` is set (door, secret, malformed); a tag in `spawn_limits.direct_edit_block_tags` (D-2852 one config point; default `[auth, secrets]` per D-2854 (3), the auth/secret tags crit (1)'s AUTH_DOOR_PATHS guard stands for; `money`, `migration`, `public-contract`, `live-step` go through the model, delta3 seat K2); `privacy=sensitive` (a feasibility guard, not an efficiency choice: the orchestrator is not a marked privacy model, D-2683, so a direct edit is not allowed at all; delta4 seat M19, D-2862: the spec's four guards are a floor, not an exhaustive list); context fill: `ctx_fill` missing or > `route_model.max_ctx_fill` (0.70); Claude quota: `quota_headroom` missing or not strictly above `route_model.quota_floor` (spec: "above the policy floor", K13); prices: the orchestrator model (`--orch-model`, copied onto the card) missing from `route_model.prices`, or no `W_run` source for the first writer leg -> `prices missing` (K1). `ctx_tokens`, `ctx_fill`, `quota_headroom` come from the orchestrator: `route`/`spawn` flags `--ctx-tokens N --ctx-fill F --quota-headroom F --orch-model M`, copied onto the card; a missing one fails the guard toward a writer |
| leg stats | pure `route_stats(records, leg, now)` over the spawner run records | `p_fail(leg)` and `dur(leg)` over the leg's real writer tasks in the last 14 days (delta3 seat K8): a task is a root record with role implement plus its continuation children (`_continue_writer` records, joined by their parent run id), counted once with the summed wall time; its leg is the last attempt's leg (`attempts[-1].leg.id`; `chosen_leg` is set only on success); it counts as success when the final status is `completed` with exit 0, as failure for `failed`, `lost`, or `cancelled` with `liveness=stalled`, and is left out for `refused`, `running`, `queued` and a hand `cancelled` without liveness; `dur` = median wall time, `p_fail` = failures / (successes + failures); tasks under 60 s and E10's test records are left out; fewer than `route_model.min_runs` (10) -> `route_model.defaults` (p_fail 0.48, dur 19 min, the D-2853 measurement over 29 runs) with `source: default`. `queue(host, leg)`: from E4's host registry once E4a lands; before that 0 with `queue: unknown` in the record (this biases toward delegation, which is the cautious side) |
| cost model | pure `decide(card, role, limits, stats, prices) -> RouteDecision` | after the guards, exactly the spec's formulas: `cost_del = P_out*T_brief + N_del*P_cache*ctx + (W_run + P_out*T_check)/(1-p_fail)`, `time_del = queue + dur/(1-p_fail) + t_fill_push`; `cost_dir = P_out*T_edit + N_dir*P_cache*ctx + carry`, `time_dir = est_lines*s_line + t_tests`, with `T_edit = 12*1.5*est_lines`, `T_check = 12*est_lines + T_report`, `carry = T_edit + T_test_out` times `P_cache` times `turns_left`. Seat-family loss (D-2855): a direct edit makes the orchestrator's family a writer family of the PR, so a seat of that family can no longer post (keeper `same_family`, correct). `cost_dir` and `time_dir` therefore add `seat_loss = sum over the seats the item needs of max(0, (cost and dur of the seat E3b picks with the orchestrator family added to the writer families) - (cost and dur of the seat it picks without))`, per seat position and per term, from E3b's `seat_options` (delta4 seat M7: a cheaper substitute seat is no gain, so the loss is clamped at 0 and never rewards the direct edit); when the orchestrator's family becoming a writer family would leave fewer eligible counted seat families than the item's tier needs, the guard reason is `no seat family left`, writer (feasibility guard, not a model input: the item could not be reviewed at all; M19, D-2862). `direct_edit` iff `cost_dir + L*time_dir <= cost_del + L*time_del`. Prices (K1): the spawner catalog has no per-token prices, so `spawn_limits.route_model.prices` is the one price table: `{model id: {in_usd_per_mtok, out_usd_per_mtok, cache_read_usd_per_mtok}}`, seeded from the providers' published prices for every orchestrator and writer model in use, with no fallback default (a missing model fails the guard above); `P_out` and `P_cache` are the orchestrator model's out and cache-read prices; `W_run` = E10a's median measured run cost of the leg (its tokens times the same table) when the leg has >= `min_runs` measured runs, else the catalog `cost_per_task_usd`, else the guard fails; the median uses only runs whose E10a tokens are `source: measured` and whose `usd` is not null, and the record keeps `w_run_source` plus the counts of measured, estimate and null-price runs left out (delta4 seat M9). Seat cost and duration (M8) come from the same E10a records with role `review` for the seat's leg (median `usd` and wall time over measured runs, `min_runs` 10), else the catalog `cost_per_task_usd` and `route_model.defaults.seat_dur_min` (L1 estimate 12, recorded as an estimate), with `seat_source` in the record. Units (K3): every token count is divided by 1e6 before it meets a per-mtok price, so every cost term is USD; every time term is converted to hours before it meets `L` (config stores minutes and seconds, named `_min`/`_s`); `L` = `route_model.latency_usd_per_h`, default 10 USD/h (D-2854, interim router value, one config place; the operator may change it); a config without the key -> guard reason `route_model.L unset`, writer. Every other constant lives in `spawn_limits.route_model`. Spec values: `T_brief` 3500 tokens, `N_del` 8, `N_dir` 3. The spec gives no value for six (K4); the L1's estimates, recorded as such in every route record so the router can retune them: `s_line_s` 1 (about 18 output tokens per line at the orchestrator's measured rate plus edit-tool latency), `t_tests_min` 5, `t_fill_push_min` 3, `T_report` 1500 tokens, `T_test_out` 2000 tokens, `turns_left` 20; validation refuses negatives, `p_fail >= 1` is clamped to 0.95 |
| fix rounds (D-2854) | `decide`, fix cards (`admission.is_fix`) | the model decides for fix cards too. `est_lines` for a fix card is the fixer estimate on the card. The 0.2d code-built brief (`build_fix_brief`, every finding verbatim) is the input either way: `route --json` prints it with a direct decision, and the orchestrator applies exactly that list, so its own reading of the seat output cannot creep in. A delegated fix keeps fixer >= writer and never flash |
| route record | `routes/<route_id>.json` in the spawner state, `route_id = route-<32hex>` | written for every `decide` call that reaches the cost model: card key, `est_lines`, both sides with each term, `L`, the stats and their sources, the constants and their sources, `mode`, `reason`. E3c also posts it to the keeper as a `log_item kind=route` entry (a new `LOG_KINDS` value in `store_fixlog.py`: keeper schema, so E3c is HIGH with a router judge, like E5's `refute`; a `route` entry changes no state and moves no round) on the item (L1 principal, append-only; body = the record minus nothing secret, capped like the other log kinds), which E1b reads. `route` prints both sides with numbers and the id; the orchestrator's Hand-Edit commit carries `Route-Decision: <route_id>` (E1 checks it above 50 lines) |
| plan field | `SpawnPlan.mode` (`"writer"` default), `mode_reason`, `route_id`; `to_dict` includes them | `route --json` shows them; legs are still listed |
| spawn refusal | `SpawnService.spawn`, after the plan, before launch | `mode == "direct_edit"` -> `SpawnRefused("direct_edit: <both sides>; make it a Hand-Edit commit with Route-Decision: <id>")`; `save_refusal` gains `refusal_code` (`direct_edit`) and the card's `key`. `--override-reason` lets the run proceed and the record keeps `mode`, `mode_reason`, `route_id` and the override text. The leg/model override branch already requires `--override-reason` (`routing.hand_picked`), so there the refusal never fires (delta2 seat H11): `decide` still runs and the record carries the decision, so the bypass is visible in metrics |

- A card that understates `est_lines` gets a direct edit whose real size the gate still bounds at 400 per commit and per item, and `lane_gates.py hand-edits` flags an `est_lines` under half the real lines.
- With today's numbers the spec expects direct to win for any fully specified change up to a few hundred lines; delegation wins when the orchestrator lacks the context, the change needs exploration, the context is near its cap, or Claude quota is short. The decision is only as good as `L` and the stats, so both are in every route record and E10's digest.

E3 tests:
- each guard failing alone -> writer with its reason (missing `ctx_fill`, `ctx_fill` 0.71, quota under floor, `L` unset, `est_lines` 401 / `true` / missing, door / secret / `./` / backslash / `..` scope entry, a door only in `write_set`, `risk_tags: ["auth"]`, sensitive);
- a fully specified fix card -> decided by the model; its route output carries the code-built brief with every finding id;
- `L` default 10 applied; a config without `latency_usd_per_h` -> writer;
- an orchestrator model missing from `prices`, and a leg with no measured runs and no `cost_per_task_usd` -> writer (`prices missing`);
- units: a hand-computed case in USD and hours, with a minutes-vs-hours regression (feeding minutes must change the answer the test pins);
- `quota_headroom` equal to the floor -> writer;
- `route_stats`: a task with two continuation children counts once; `cancelled` without liveness is left out; `lost` counts as a failure;
- `risk_tags: ["money"]` -> decided by the model; `["auth"]` -> writer;
- the cost model against a hand-computed table: a 50-line change with ctx 150k and default stats -> direct; the same with `p_fail` 0.05, `dur` 5 min and `L` 0 -> writer where the formula says so; the boundary case `cost_dir + L*time_dir == cost_del + L*time_del` -> direct;
- `route_stats`: fewer than 10 runs -> defaults with `source: default`; test records excluded;
- the route record holds every term and is referenced by the plan's `route_id`;
- the refusal on the routed path, proceeding with `--override-reason`, and the leg override branch recording the decision without a refusal;
- `route --json` carries `mode`, `mode_reason`, `route_id` and both sides.

### E3b seat-family selection (D-2855)

Seen 2026-10-25 (REVIEW-METHOD-SETTING): a Claude Hand-Edit made anthropic a writer family, the Haiku seat then got keeper 409 `same_family`, and the L1 had to ask the router for another seat. The router refused a size-based exemption, so the family rule stays and seat choice becomes code.

| piece | where | behaviour |
|---|---|---|
| writer families | `gates.seats.writer_families(item, commits)` | the same set the keeper enforces at verdict time, by construction (delta4 seat M1-M3, M5): the per-commit rule is factored out of `keeper/coverage.py:writer_families` into one pure function `commit_family(message, changed_paths)` that both call: a `Writer:` trailer gives its model's family (the keeper's regex table, `review/family.py`), and on a dual-trailer commit only the Writer counts; a Writer-less `Hand-Edit:` gives `anthropic`, except the keeper's proven changelog-only edit; an unknown model, a bare `Writer: run-<32hex>` or a trailer-less commit raises `writer_unknown` exactly as the keeper does, and `pick_seats` refuses (`SeatRefused("writer_unknown")`), never guesses. The commit set is the keeper's: every commit behind the item's current-epoch verified commit+ci evidence plus the PR's own `BASE..HEAD`, and with no commit evidence the keeper's fallback chain (writer_model, claimant, delegate families) |
| order | `spawn_limits.seat_order` (one config place), default `[haiku (anthropic), qwen3.8-flash via qodercli (alibaba), mimo-v2.6-pro (xiaomi), glm-5.3-flash-free via opencode (zhipu)]` (D-2855) | each entry: leg id, family, `sole_high: bool` (default `true`; qwen `false`: never the only seat on a HIGH item; delta4 seat M6) |
| pick | pure `pick_seats(order, writer_families, tier, n, unavailable) -> [leg]` | `n` is not a free input (delta4 seat M4): `tier` is the keeper item's review tier (`standard` or `high`, fixed at definition time, read from the item, never from the card), and `n` = 2 for `high`, 1 for `standard`; a caller `n` that differs is refused. It walks the order and takes the first `n` legs whose family is not a writer family, differs from the families already taken, and is not in `unavailable` (rate-limited/down legs from the host's leg status, recorded); a HIGH set must contain at least one leg with `sole_high: true` (with today's order this only bites if several `false` legs are configured) and at least one non-Claude leg (auth rule). No valid set -> `SeatRefused("no eligible seat family")`, never a silent smaller set |
| route | `route --role review` and `spawn --role review` with no `--leg` | call `pick_seats` with the PR's writer families (`--range BASE..HEAD` or `--writer-families`), so the L1 no longer asks the router for a seat; an explicit `--leg` still needs `--override-reason` and is checked against the family rule |
| seat_options | pure `seat_options(order, writer_families, tier, extra_family, stats, prices)` | the cost and median duration of the picked set with and without `extra_family` added to the writer families; E3's seat-loss term uses it |

E3b tests:
- a Hand-Edit commit adds anthropic: Haiku is skipped, qwen + mimo-pro picked for HIGH;
- qwen alone is never the HIGH set; a HIGH set without a non-Claude leg is refused;
- an unavailable leg is skipped and recorded;
- writer families anthropic + alibaba + xiaomi on a HIGH item -> refused (only zhipu left), and E3's guard gives `no seat family left`;
- `seat_options` difference matches a hand-computed case, and a cheaper substitute seat gives `seat_loss` 0, never negative (M7);
- parity (M15): over one shared fixture set (Writer-only, Writer-less Hand-Edit, changelog-only Hand-Edit, dual-trailer commit, non-Claude author trailer, `Writer: run-<32hex>`, unknown model, no commit evidence with claimant/delegate fallback) `gates.seats.writer_families` and the keeper's `writer_families` give the same set or the same refusal, and every set `pick_seats` returns passes `_reviewer_family_ok` against a fake keeper item;
- a caller `n` that disagrees with the item tier is refused (M4).

E3c tests (delta4 seat M17): with a fake keeper, a `decide` that reaches the cost model writes `routes/<id>.json` and posts one `log_item kind=route` entry whose body equals the record; the keeper accepts `kind=route` from the L1 principal, refuses it from a seat principal, and moves no state or round; `spawn` refuses `direct_edit` with `refusal_code` and key in the refusal record; `--override-reason` proceeds with `mode`, `mode_reason`, `route_id` kept; the trailer id of a Hand-Edit built from `route --json` equals the keeper entry id (K13 parity).

### E4 host admission + per-project slots (crit 4)

Split in two for size (seat r1 E14): E4a carries the host admission row of PG-EFFICIENCY-GATES 4c plus the host registry and lock; E4b adds slots, reservation and the queue.

The 4c host admission row moves to E4a unchanged: dedup keys, pid + start-time match, client process patterns including agy `-p` (D-2823), projected RSS p90 never below the harness default, `floor_mb` 2000, and the `host_full` refusal at all three launch sites. The additions:

| piece | slice | where | behaviour |
|---|---|---|---|
| host registry | E4a | `~/.local/state/plangraph/host/` (env `PLANGRAPH_HOST_DIR`): `runs/<run>.json {run_id, project, role_class, pid, start_time, host, admitted_at}` and `queue/<run>.json {run_id, project, role_class, created, pid, start_time}` | host-wide and shared by every spawner state dir (each L1 passes its own `--state`). An entry is stale when its pid is gone, or its start time is readable and differs, or it is older than `max_cap_min + 30` min. When the pid is alive but the start time cannot be read (e.g. a host without `/proc`), the entry counts: occupancy may be over-counted, never under-counted (seat r1 E10). `project` = the card key prefix; no key -> `unkeyed` |
| atomic admit | E4a | `host_load.admit_host(probe, limits, entry)` under an exclusive lock on `host/lock` (`fcntl.flock`; `msvcrt.locking` on Windows) | count, decide and register in one critical section. If the lock cannot be taken within 10 s, or the lock call raises (no locking support, read-only dir), admission refuses with `host_lock`: fail closed, never unlocked (seat r1 E10). The probe (meminfo, `/proc`, start times) is injected; a host where it is unavailable gets the `unknown` path above |
| writer and seat slots | E4b | `spawn_limits.max_runs` (4) stays the total; new `max_seat_runs` (2) | seats (`role_class=seat`) count against `max_seat_runs` and never take a writer slot; writer/default runs count against `max_runs - max_seat_runs`. Client processes with no registry entry count as unattributed writer-slot users. The memory floor covers all runs together |
| queue first, then admit | E4b | every spawn writes its `queue/` entry under the lock BEFORE the first admission check | queued work is visible from the first moment, so no fresh spawn bypasses waiting projects (seat r1 E5) |
| order | E4b | same critical section | among the live queue entries of the caller's slot class, the next to be admitted is the entry with the smallest key `(running runs of its project in that class, created, run_id)`; `run_id` makes the order total, so a `created` tie never leaves two entries each believing they are first (delta2 seat H13). The caller is admitted iff a slot is free AND its entry has that smallest key. A project with no running run therefore always comes before one that has a run, and ties go to the oldest entry across projects. With a finite number of projects, every entry eventually has the smallest key, so the order is starvation-free. Nobody holds the lock while waiting, so it is deadlock-free |
| queue wait | E4b | `spawn` polls `admit_host` every `queue_tick` (`spawn_limits`, default 5 s, injected sleep) for up to `queue_wait_min` (default 60; 0 = one check, as today) | each poll refreshes the entry's `polled_at`. Sweeper (delta2 seat H13): every `admit_host` call, inside the lock, first drops queue entries whose owner pid + start time is dead or whose `polled_at` is older than `3 * queue_tick`, so a wedged or killed owner holding the smallest key idles a free slot for at most three ticks, not until the `max_cap_min + 30` age rule. Expiry -> `host_full` refusal. The entry is removed on admit, expiry, refusal or exit. The L1 shell loops (`hostgate.sh`) are no longer needed |

E4 invariants:
- I-E4a: among waiting entries of a class, a project with no running run in that class is admitted before any project with one; ties go to the oldest entry.
- I-E4b: a seat never takes a writer slot, and a writer never takes a seat slot.
- I-E4c: admission happens only inside the host lock; a lock failure refuses.
- I-E4d: stale entries never count; an entry whose freshness cannot be read counts.

E4a tests:
- 4c's dedup, agy `-p` and pid-reuse cases;
- a lock that raises -> `host_lock`;
- an unreadable start time with a live pid -> counted;
- an old entry -> stale;
- two admit calls racing under the lock -> one admitted;
- a run ended by a stall kill (fake runner) leaves no `runs/<run>.json` entry.

E4b tests:
- project A holds both writer slots while B is queued -> B is admitted first when a slot frees;
- a fresh A spawn arriving after B's entry -> still behind B;
- three projects -> round-robin order;
- seats admitted while the writer slots are full;
- an unattributed client process takes a writer slot;
- queue expiry -> `host_full`;
- a smallest entry whose owner is dead or stopped polling -> swept within three ticks, the next entry admitted;
- two entries with equal `created` -> exactly one is first (by `run_id`);
- a writer never takes a seat slot when writer slots are full and a seat slot is free (I-E4b);
- cross-row (delta2 seat H18): an E2 stall kill removes the run's host registry entry and frees its slot (IE3 with I-E4c), tested in E4a once the registry exists.

### E5 seat parsers (crit 5, DECISIONS 3.4)

| piece | where | behaviour |
|---|---|---|
| one verdict extractor | `gates/findings.parse_seat_verdict` is the single extractor. `tools/seat_verdict.parse_verdict` calls it and maps the result to its `(ok, idx)` shape and exit codes (seat_verdict already imports `src`, seat r1 E12) | the line and word rules are unified: colon optional, and the fix words are the exact union of `seat_verdict.FIX_WORDS` and `findings._CHANGE_WORDS` (so `block`, `request_changes` and `request-changes` stay, delta2 seat H4) plus `changes_requested`; the fixture table is generated from both old sets, so a word dropped from either fails the test; pass words add `accept(ed)`/`ACCEPT` and `approve(d)` (D-2844). A shared fixture table covers the seat's reproduced mismatches: `VERDICT fix - LOW only:`, `VERDICT: failed - LOW only:`, `VERDICT: changes_requested`, plus `VERDICT REQUEST_CHANGES`, `VERDICT: ACCEPT`, `Verdict accept` (colon-less) |
| LOW-only variants | `findings.low_only(verdict_line, body)` | the VERDICT line has a fix word followed, on the same line, by `LOW only` / `LOWs only` / `only LOW(s)` (case-insensitive, any punctuation between, e.g. "fix - LOW only:"). It counts as pass-with-lows only when the body has no line carrying any `findings.BLOCKING` severity token (`HIGH`, `MED`, `CRITICAL`, `BLOCKER`, `MAJOR`; delta2 seat H1: a "CRITICAL:" line must never ride a LOW-only verdict), by a strict scan that ignores `_CLOSED` (a line such as "MED: the FIXED-flag check ..." blocks the grant, seat r1 E3). Otherwise it is changes (0.2c). `seat_verdict` posts `ok=true`, reason prefix `pass-with-lows` and the LOW lines. The same `low_only` is applied in `findings.seat_outcome` before its fix-word mapping, so `first_review` and the round gate grant the same pass-with-lows (delta2 seat H2) |
| one seat decision | `findings.seat_decision(output) -> (outcome, reason)` | the refusal rules both sides use today are merged into this one function (delta2 seat H3): failed/billing/fence output, the `_FAILED` short-body rule, `status`/`is_error`, `finish_reason` other than a normal stop, and `test_gaming`; then the verdict line, `low_only` and refutations. `seat_verdict` (exit codes) and `seat_outcome`/`first_review` (round gate) are thin adapters over it, so one seat output cannot be a pass in one and a re-run in the other |
| refutation held by the keeper | keeper `log_item` gains kind `refute`, router principal only (`store_fixlog.py`; the other kinds stay router/l1) with body `{decision: "D-<n>", seat: "run-<32hex>", lines: [{id, path, line, reason}]}`. Shape (delta2 seat H15): `normalise_entry` gets its own `refute` branch: `decision` matches `D-[0-9]+`, `seat` matches the run-id format, 1..50 `lines`, `id` <= 16 chars, `path` <= 300 chars repo-relative, `line` int >= 1, `reason` <= 500 chars; anything else 400. A `refute` entry changes no state, hands nothing back from `in_review`, and moves no round (`rounds.count_rounds` ignores it on purpose, stated in code and pinned by a test). Seat binding (delta2 seat H16): `seat` is a run id only, never a seat dir, and it must be a `review` run recorded on this item by the completion sink (keeper run events, a keeper fact); `seat_verdict --run-id` must equal it, and evidence.json or `--seat-dir` names are not trusted for the binding. `tools/seat_verdict.py --refutations-from-keeper` reads the item's `refute` log entries through the keeper API | binding to the router's keeper principal replaces the markdown decisions-log check (seat r1 E1: an L1 could write its own log file). A refutation counts only when ALL hold: the entry's principal is the router (a keeper fact, not a file); it names this seat's run id or seat dir; it covers every id that the strict scan (above) finds with a `BLOCKING` severity in the seat body; each `path` exists at `--head` (`git cat-file -e`) and `line` is within that file. Then `ok=true`, reason `pass-with-lows: refuted per D-<n> (keeper log <event id>): <ids>`. A missing keeper, no entry, or any failed check -> exit 2 (refused), nothing posted. D-2839's `Refutes: <project:ID>` token moves into the router's keeper entry as `decision` + `seat` |
| round gate | `findings.apply_refutations(outputs, refutations)`, used by `first_review` and `seat_outcome`; `tools/lane_gates.py seats --refutations-from-keeper` | the same refutation rule turns a refuted seat into pass-with-lows in the round gate too (seat r1 E2), so `first_review` and `seat_verdict` agree by construction |
| no re-run | unchanged | the seat's text is never edited, and the refutation is a separate keeper entry; 3.4's "never re-run a seat with rulings in its prompt" is untouched |

E5 tests:
- the shared fixture table: both entry points give the same answer on every row, the three reproduced mismatches included;
- `fix - LOW only:` with LOW lines -> pass-with-lows in `seat_verdict` AND `seat_outcome`/`first_review`;
- `fix - LOW only:` with a "CRITICAL:" or "MAJOR:" line -> changes;
- `VERDICT: request_changes`, `VERDICT: block` -> changes;
- `VERDICT: pass` + "quota exceeded" short body, and a `test_gaming="yes"` seat -> the same refusal from both adapters;
- the same with "MED: the FIXED-flag check ..." -> changes;
- `VERDICT: FIX` with LOWs and no "only" -> changes;
- a router `refute` entry that covers every id -> pass-with-lows in both `seat_verdict` and `first_review`;
- refused cases:
  - an `l1` principal writing `refute` -> 403 at the keeper;
  - an entry for another seat run, a seat dir instead of a run id, and a run id not recorded as a review run on this item;
  - a malformed `refute` body (bad D-id, 51 lines, non-int line) -> 400; a `refute` entry leaves state and round count unchanged;
  - an uncovered id;
  - a missing path;
  - a line past the end of the file;
  - keeper unreachable.

### E7 runs outlive sessions (crit 7)

Today a run's supervisor is the `spawn --wait` CLI process (or the MCP server), a child of the orchestrator session: when the session ends, the supervisor dies with it, the run's result is never written, and nobody learns the outcome (seen 2026-10-25: background seat runs lost with their session).

| piece | where | behaviour |
|---|---|---|
| detached supervisor (E7a) | `cli.py spawn --detach` (default for `implement` and `review` from E7a on; `--attach` keeps today's behaviour) -> new verb `supervise <run_id>` | `spawn` admits, writes the record `status=queued` with `owner`, then launches `python3 -m plangraph.spawner.cli --state <state> supervise <run_id>` outside the session. With a reachable user manager (E7a's own probe, injected) the launch is `systemd-run --user --unit=pg-sup-<12 hex> --collect --quiet -- ...`, so the session's scope stop cannot reach it; on logout the user manager stops it unless lingering is on, so the probe also reads `loginctl show-user --property=Linger` and records `supervisor: systemd (linger=no)` as a warning, and the reaper below covers the stop. Otherwise, on POSIX, `subprocess.Popen(start_new_session=True)` (recorded `supervisor: setsid`); on `os.name == 'nt'`, `Popen(creationflags=CREATE_NEW_PROCESS_GROUP \| DETACHED_PROCESS)` (recorded `supervisor: nt-detached`; `kill_process_tree` keeps `taskkill /T /F` for the run tree, which no longer contains the supervisor). stdio goes to the run dir |
| start handshake (E7a) | `spawn --detach` | keeps today's guard (cli.py: block on the child's first record line, raise when the child dies first), bounded by `supervisor_start_s` (default 20): the supervisor's first act is to write `supervisor: {pid, start_time, mode}` and `status=running`; if that does not appear in time, `spawn` kills the child, writes `status=failed`, `error=supervisor_never_started` and returns non-zero. So `--detach` returns after the handshake, not "at once", and no queued record is left without a supervisor |
| supervisor (E7a) | `supervise` | runs `SpawnService.execute` for the record (monitor, E2 stall kill, 4a result files), then delivers the completion; an exception still writes `status=failed`, `liveness=None`, `error` before exit. A second `supervise` for the same run exits at once while the first holds `runs/<run>/supervisor.lock` (flock / msvcrt as E4); a lock whose holder pid + start time is dead is stale and may be taken |
| reaper (E7a) | `SpawnService.reap_orphans()`, run by every `spawn`, `watch` and `live_state` call (no daemon) | a record with `status in (queued, running)` whose recorded supervisor pid + start time is dead and whose lock is free is an orphan: the reaper takes the lock, kills any leftover run tree (`kill_process_tree` on the recorded run pid if its start time still matches), and writes `status=failed`, `exit_class=lost`, `liveness=supervisor_gone`, `error=supervisor died`, then delivers that completion like any other. So a supervisor killed by OOM, reboot or logout still ends with exactly one completion. No restart: the run's work is lost and the L1 decides (4a2 fall-through applies to `lost` like `failed`). `lost` is added to the keeper `EXIT_CLASSES` vocabulary |
| owner (E7b) | record field `owner` = `--owner`, else `PLANGRAPH_OWNER`, else the 4a3 principal name; `claim = {key, epoch}` from the card (`claim_epoch`) when given | completion events go to recipient `owner` through the existing `FileInbox` (`state/inbox/<owner>.jsonl`) and, as today, to the keeper outbox. One run identity (seat r2 G7): `idempotency_key = run_id` (no status suffix), and `--ack` takes the same key; `SpawnService.cancel` refuses a record that is already terminal, so a finished run never gets a second, cancelled event. Events and the keeper post carry `claim: {key, epoch}`; `KeeperSink.deliver` posts the epoch and the keeper's `record_run` refuses a run event whose epoch is not the item's current one (`epoch_stale`), which the sink records as `delivered: stale_claim` (the owner still gets the inbox event) |
| watch (E7b) | `cli.py watch --owner X [--follow] [--ack <run_id>...]` | runs the reaper, then prints the owner's pending events (inbox lines whose `idempotency_key` is not in `inbox/<owner>.acked`) and its runs in three states: `live` (supervisor pid + start time match), `starting` (`queued`, younger than `supervisor_start_s`), and nothing else, because the reaper has already closed every orphan. `--follow` polls every `tick` (injected sleep) and prints each new event once. `--ack` appends keys under the inbox lock. A relaunched session runs `watch --owner <me>` once and sees every completion it missed |

E7 tests (E7a):
- the `supervise` argv for systemd, setsid and nt launch modes (captured, no real systemd; `os.name` injected);
- a child that dies before the handshake -> `supervisor_never_started`, non-zero, no queued record left;
- a supervisor exception writes `failed`;
- a second supervisor exits while the lock is held; a stale lock is taken;
- reaper: dead supervisor + free lock -> `lost`/`supervisor_gone` + one completion; a live supervisor is untouched; pid reuse (same pid, other start time) counts as dead.

E7 tests (E7b):
- events reach `inbox/<owner>.jsonl` once (replayed delivery deduped on `run_id`);
- cancel of a finished record is refused, so no second event;
- `watch` lists pending events and hides acked ones;
- `--follow` with fake sleep prints each new event once;
- a run event with a stale epoch -> `delivered: stale_claim`, inbox event still present.

Open fact F3: whether ending a Claude Code session stops its process group or its whole cgroup. The design covers both (systemd-run when available). Before E7a merges, one detached run must outlive a killed session, as a manual check recorded in the PR.

### E9 claim heartbeat (crit 9)

| piece | where | behaviour |
|---|---|---|
| heartbeat | the E7 supervisor loop (same tick as E2's monitor) -> keeper verb `heartbeat(key, epoch)` (`keeper/store_verbs.py`, exists: extends `lease_until`, never shortens, renews the run token via `_extend_run_token`) | when the record has `claim`, the supervisor heartbeats every `heartbeat_s` (`spawn_limits`, default 300) while the run lives. Lease check (seat r2 G3): admission reads the claim's actual `lease_until` (returned by `claim`, carried in the card as `claim_lease_until`; a card with `claim` but no lease is refused) and refuses when `heartbeat_s >= (lease_until - now) / 3`; `whoami` has no lease field and is not used. Credential (G2): at spawn the owner mints a writer run token for this run (`mint_run_token(key, "writer", run_id)`, existing keeper verb, bound to key + epoch, admitted by `_actor_or_delegate`); `spawn` writes it to `runs/<run>/run_token` mode 0600, the supervisor heartbeats with it, and the file is deleted at completion. The shared `PG_TOKEN_FILE` spawner identity is never used for heartbeats (the keeper would refuse it). Responses (G1): a 2xx extends; any keeper refusal (`epoch_stale`, `forbidden`, `bad_state`, `conflict`, `not_found`) means the claim is lost: record `claim_lost: <code>` once, send one owner event `CLAIM_LOST`, delete the token, stop heartbeating and keep the run (the result may still be useful; E7b's epoch guard stops it landing on a new claim). Only transport errors (no response, 5xx) retry next tick, counted in `heartbeat_errors`; once the local clock passes the last known `lease_until` with errors still failing, that is also `claim_lost: lease_expired` |
| no shell loops | docs only | the per-session shell heartbeat loops are retired once E9 merges; the router announces it |

E9 tests:
- a fake keeper client sees heartbeats at the fake-clock interval only while the run lives;
- each refusal code (`epoch_stale`, `forbidden`, `bad_state`, `conflict`, `not_found`) -> `claim_lost` + one `CLAIM_LOST` event, no further heartbeat, the run continues;
- a transport error -> a retry, counted; errors past `lease_until` -> `claim_lost: lease_expired`;
- the heartbeat uses the run token file, never the spawner token; the file is gone after completion;
- a card with `claim` but no `claim_lease_until` is refused;
- no `claim` -> no heartbeat;
- a `heartbeat_s` too long for the claim's remaining lease (including a 60 s lease) is refused at admission.

### E6 compiled boot file per level (crit 6)

| piece | where | behaviour |
|---|---|---|
| levels on rules | keeper rule items (slice 2, `rule_set`) gain an optional `levels` list from `{L0, L1, L2, writer, seat}`; missing = every level | a real field (seat r2 G11): `store_rules._rule` admits `levels` (unknown values refused 400), stored in the rule item's JSON body next to `depends_on`; `decisions_view` renders it as a trailing `[levels: L1, writer]` tag and `parse` reads it back, so the DECISIONS.md round trip (I7) keeps it. Superseded rules (G12): `rule_set` gains `supersedes: [id]`, which retires the named rules in the same write, so a superseding rule cannot leave its predecessor live by accident. Retired rules are never rendered |
| boot staleness | `tools/boot_file.py check` | the header names the export's last `rule_set` event id; `check --keeper` compares it with the keeper's current one and fails when they differ, so a boot file built before a retirement fails CI instead of leaking the old rule |
| compiler | `src/plangraph/boot_file.py` `compile(items, level) -> str` (pure), `tools/boot_file.py build --level L1 [--from-export <json>] --out <file>` | renders the live rules for that level, in rule-number order, each as `<number> <title>: <text>` with its `[gate: ...]` tag kept; D-ids are left in the text as written (seat r2 G9: no cut, since the stored order is appearance order and a cut would rewrite prose); size is held by the cap below; a header names the export sha/time; deterministic (same export -> same bytes) |
| size cap | `tools/boot_file.py check <file> --level L1` | fails above `BOOT_CAP_BYTES = {L0: 24000, L1: 16000, L2: 12000, writer: 6000, seat: 4000}`; plangraph CI runs `check` on the fixtures. The boot files live in the routing repo (G10), so E6 also ships the routing CI step as a file in this PR (`tools/boot_file.py ci-snippet` prints it) and the merge needs the routing side's PR with it open; E6 is not marked done until that routing PR has merged (operator=false leaf, gate edge E6 -> routing boot CI). Routing CI check spec (D-2850, router owns that PR): one job on every push and PR touching `boot/**` or `DECISIONS.md`, running for each `boot/<level>.md` (level in `L0 L1 L2 writer seat`, a missing file fails) `python3 tools/boot_file.py check boot/<level>.md --level <level> --keeper $KEEPER_URL` with plangraph pinned to the merged E6 sha; it fails when the file is over `BOOT_CAP_BYTES[level]`, when its header's `rule_set` event id differs from the keeper's current one, or when the keeper cannot be read (fail closed, exit 2, never skipped); no auto-rebuild in CI, the router rebuilds with `build` and commits |
| relaunch | `tools/boot_file.py relaunch --level L1 --card <card.md>` | prints the relaunch prompt = boot file + card, and nothing else; the router's relaunch path uses it, so a relaunched session reads only these two |

E6 tests:
- compile drops cancelled rules and keeps level-matching ones;
- `levels` survives parse -> to_rules -> render; an unknown level is refused;
- `supersedes` retires the named rule in the same write; `check --keeper` fails on a stale header;
- a rule without `levels` appears in every level;
- the output is byte-stable;
- `check` fails at cap + 1 and passes at cap;
- `relaunch` output = boot + card exactly.

### E8 live-state command (crit 8)

| piece | where | behaviour |
|---|---|---|
| command | `tools/live_state.py --owner X [--repo owner/name ...] [--json]` over pure `src/plangraph/live_state.py` `collect(readers) -> dict` | sections: `runs` (runs the E7a reaper first, then the owner's spawner records in `live`/`starting` plus E4's host registry, plus `lost` records not yet acked, so a run of unknown outcome is shown, seat r2 G13), `prs` (`gh pr list --author <gh login of the owner, from the owner map in the host file; no map entry -> section unavailable, never `@me`> --json number,headRefName,isDraft,mergeable,state`), `ci` (`gh pr checks <n> --json name,bucket` per open PR), `claims` (keeper `list` of items claimed by the owner, with lease and epoch). Each reader is injected; a reader that fails gives `{"unavailable": "<reason>"}` for its section, never an empty list, so a handoff never mistakes "no data" for "nothing open" |
| handoff | the markdown output is at most 60 lines | a handoff card holds decisions + next steps and the line `state: tools/live_state.py --owner X` instead of copied run/PR lists |

E8 tests:
- fake readers for all four sources -> stable markdown and JSON;
- one reader raising -> that section `unavailable`, the others intact;
- a `lost` unacked run is listed; an owner without a gh login map entry -> `prs` unavailable;
- the 60-line cap truncates with a count.

### E10 efficiency metrics (crit 10)

| piece | where | behaviour |
|---|---|---|
| tokens per run (E10a, before E3a) | 4a's `result.json`, the run record and the completion sink's keeper run post gain `tokens: {input, output, usd, source}` (`usd` from `route_model.prices`; a model missing there gives `usd: null`, never 0) | `source=harness` when the client reports usage (opencode `--format json` step events, qodercli/agy usage lines: open fact F4, verified per harness against one real run each before its parser is enabled), else `{estimate: estimate_tokens, source: "estimate"}` |
| test records excluded | `tools/metrics.py` `is_test_record(record)`, also used by E3a's `route_stats` | a record is a test record when any holds (delta3 seat K7): `status == dry_run`; no `exit_code` on a terminal record; a leg harness of `mock` or `fake`, or a leg id starting with `mock-`, `fake-` or `test-` (the names the test suites use, pinned by a grep test over `tests/`); `cwd` under the system temp dir (pytest `tmp_path`); the record has `test: true` (#214's hermetic state dirs write it). D-2844 counted 149 of 233 production records as test records. The proof is hermetic (delta4 seat M18): a recorded fixture of about 20 anonymised record shapes (each test-record rule, plus real runs that must stay in) sits in `tests/fixtures/`, and the leg-name prefixes live in one constant `TEST_LEG_PREFIXES` that the grep test checks against every leg id the test suites create; a one-off count on the production dir goes into the PR text only, never into a test. Fixtures count against the slice's 300 lines The routing side runs its own metric from 2026-10-25 and E10 replaces it later |
| tokens per merged change | `tools/metrics.py tokens --since <iso> --repo <path>` | merged change = a PR merged into main since `--since` (`gh pr list --state merged --json number,mergeCommit,commits`, so merge, squash and rebase merges all count, seat r2 G15); its commits are the PR's commits. For each, take every `Writer:` trailer: a `run-<32hex>` id is summed from the run's tokens; a trailer without that id counts as `unattributed`, a commit with `Hand-Edit:` as `hand_edit`, a commit with neither as `no_trailer`, each shown as a count, never as 0 tokens (G14). Run tokens come from the keeper's run events (`record_run` / `items.run_ids`, which the completion sink already posts from every host; E10 adds `tokens` to that post), not from local state dirs (G16); seat runs count by item key the same way. Reports per item and the total, with the share of `estimate` sources and the unattributed counts shown |
| claim-to-merge | `tools/metrics.py cycle --since <iso>` | per item: first `claimed` event -> `mergedAt` of the last merged PR whose commits carry its `Plan-Item:` trailer (same PR list as above); an item with a claim and no mapped PR is listed as `unmapped`, not dropped; median and p90 |
| digest | `tools/metrics.py digest` prints both tables in the router digest format (markdown, at most 30 lines) | the router pastes or pipes it into its digest; wiring it into the routing digest job is a router follow-up |

E10a tests (delta3 seat K9):
- each harness parser on a recorded fixture (one real run's usage output per harness, captured once, scrubbed, stored under `tests/fixtures/usage/`) -> the expected token counts; a parser stays disabled until its fixture exists (F4);
- `usd` from the price table; a model missing there -> `usd: null`;
- no usage output -> `{estimate, source: "estimate"}`;
- the three sinks (record, `result.json`, keeper post with a fake keeper) carry the same `tokens`.

E10 tests:
- fake run records + a fake git log -> tokens per item, with estimate shares;
- a Writer trailer without a run record is counted as `missing`; a name-only trailer as `unattributed`; a Hand-Edit commit as `hand_edit`;
- a squash-merged PR and a rebase-merged PR both count; an unmapped item is listed;
- cycle time from a fake event log;
- the digest stays within 30 lines.

## Invariants

| id | invariant |
|---|---|
| IE1 | No Hand-Edit (gate) and no `direct_edit` route ever touches `AUTH_DOOR_PATHS`, a secret-shaped path or a path that does not canonicalise; route also sends any tag in `direct_edit_block_tags` to a writer; route's ceiling is never above the gate's (both `doors.HAND_EDIT_MAX_LINES`). |
| IE2 | Route mode is a pure function of (card, role, limits, stats, prices); a missing or malformed guard input (`est_lines`, scope, `ctx_fill`, `quota_headroom`, `L`, the orchestrator's price entry, a `W_run` source) yields `writer`, never `direct_edit`; every cost-model decision has a route record and a keeper `route` entry. |
| IE3 | A stall kill always leaves `exit_class=stall`, `liveness=stalled` in the record and `result.json`, and no runner job in `running`. |
| IE4 | Host admission is atomic under one host-wide lock, and stale registry and queue entries never count (I-E4a..d). |
| IE5 | A pass-with-lows needs either a VERDICT line that says LOW only with no HIGH/MED line in the body (strict scan), or a router-principal keeper `refute` entry bound to this seat's recorded review run that covers every strict-scan `BLOCKING` id with a valid path:line; `seat_verdict` and `first_review` call the same `seat_decision`. |
| IE7 | A run's result and completion event never depend on the spawning session staying alive; every completion reaches its owner's inbox exactly once (deduped on `run_id`), and `watch` shows it until acked; a run whose supervisor died ends as `lost` with one completion; a result under a stale claim epoch never lands on the item. |
| IE8 | Only the supervisor of a live run heartbeats its claim; a lost claim stops the heartbeat and is reported, never silently retried into a new epoch. |
| IE9 | Boot files contain only live rules for their level, are byte-stable for one export, and stay under their CI cap. |
| IE10 | A live-state or metrics source that cannot be read is reported as unavailable or missing, never as empty or zero. |
| IE6 | All decisions are pure functions of injected inputs (clock, sleep, probe, git reader, host dir) and are tested hermetically: no real models, systemd, network or memory pressure. |

## Input classes (E3, E5)

| input | class | result |
|---|---|---|
| card without `est_lines` or `paths` | legacy card | writer, reason `missing est_lines/paths` |
| `est_lines` 401, or a non-int | over ceiling / malformed | writer |
| no `--ctx-fill` / `--quota-headroom`, or fill 0.71 | guard input missing / over | writer |
| 51-line Hand-Edit without `Route-Decision:` | unaudited direct edit | gate refuses |
| `paths` with `keeper/`, `.github/`, `seat_verdict.py`, `vault/` | door / secret | writer |
| fix card | fix round | cost model decides (D-2854); input is the 0.2d code-built brief either way |
| `VERDICT: fix - LOW only:` + LOW lines | LOW only | pass-with-lows |
| `VERDICT: fix - LOW only` + a MED line | contradiction | changes |
| refutation written by an L1 (not the router principal) | invented ruling | refused (keeper 403) |
| router refutation for another seat run | reused ruling | refused |
| `VERDICT: fix - LOW only` + "MED: the FIXED-flag ..." | hidden MED | changes |
| `est_lines: true` | bool as int | writer |
| door path only in `write_set` | hidden scope | writer |

## Follow-on work for what the slices above do not fix (D-2844, routing report §6; operator asked to work these, 2026-10-25)

### E11 wake budget (wake policy, slice 12)

80-90 % of orchestrator turns are wakes. Each one re-reads 150-190k of context, and most carry nothing to decide: a seat finished, CI went green, a writer finished while its seat is still pending. Today each background `spawn --wait` or poll loop of an L1 ends in its own wake.

| piece | where | behaviour |
|---|---|---|
| batch wait | `cli.py watch --owner X --block --batch <run>[,<run>...] [--wake-on <classes>] [--max-wait-min N]` (on E7's inbox) | blocks without waking anyone until (a) an event of a wake class arrives, or (b) every run of the batch has an event, or (c) `--max-wait-min` (default 120) passes. It then prints ONE digest: one line per event, at most 40 lines, plus `pending: <runs>`. Default wake classes are the ones that need a decision: `FAILED`, `CLAIM_LOST`, `REFUSED`, and a seat completion whose parsed verdict is changes with a HIGH. A plain `DONE` or a seat PASS never wakes on its own. The L1 starts one background `watch --block` per batch instead of one wait per run |
| digest parsing | same, using `gates.findings` for seat runs (4a `result.json` + stdout.log) | each seat line shows `PASS` / `changes (H/M/L counts)` / `no verdict (reason)`, so the woken L1 acts without opening the files |
| router side | not in plangraph | peer messages that need no action (acks, FYIs) are the router's part; recorded as a routing follow-up |

E11 tests:
- a batch of 3 runs with only DONE events -> one wake when the last arrives;
- a FAILED event mid-batch -> an immediate wake, with the others listed as pending;
- a seat changes with a HIGH -> wakes, a seat PASS does not;
- the max-wait expiry;
- the digest cap.

E11 depends on E7b. Order: right after E9. Budget 250.

Moving the whole standard-tier loop (writer -> CI -> seat -> fix -> merge) into code is the E2E/phase-runner path. That is operator question Q-INV-1, not this item.

### Base chain (no code; L1 + router steps)

E5 (keeper `refute`), E6 (keeper rule items) and E9 (live `heartbeat`) need #203 merged and the live keeper redeployed from main. Today's live keeper is 828 commits behind. The steps:
1. #203 green: the 5 old failures are with the D-2832 fixer.
2. #203 seats and phase review.
3. Merge with a router GO.
4. Keeper redeploy from main with a router deploy GO, as a recreate (live-tree merge safety rule).

The L1 asks routing for the GO D-ids at steps 3 and 4. Until step 4, E5/E6/E9 are built and tested hermetically but marked `needs live keeper >= <sha>` in their PRs.

### Capacity

| step | what | owner |
|---|---|---|
| MCP off for writer/seat runs (D-2846) | PG-EFFICIENCY-GATES slice `#9` 4a-mcp: `overlay_env(base_env, mcp_off=())` adds `{"mcp": {name: {"enabled": false}}}` for every MCP server name in the effective config (`~/.config/opencode/opencode.json` and `config.json` `mcp` keys, the AutoOS root overlay `mcp.servers`, plus the fixed five: context7, graphify, omnigraph, playwright, serena); the runner passes it for writer and seat roles (`spawn_limits.mcp_off`, default true). Measured 2026-10-25 on coding.vm: each `opencode run` tree carries ~680 MB RSS of these five servers (serena 118 MB, graphify 85, context7 155, omnigraph 139, playwright 182, incl. npm/uv wrappers), so 4 runs free about 2.7 GB; host-wide, 183 MCP processes held 9.4 GB | build-L1-efficiency, right after the 4a fix round |
| fallback legs (D-2822: a failed first leg ends the run) | PG-EFFICIENCY-GATES 4a2 fall-through | in the order above |
| restore the workstation as a run host | host operation: needs the operator (host changes are outside the router delegation) | operator, on return |

## Open facts

- F1 (E2): whether the seat harnesses stream stdout before they exit. Until verified, no seat is stall-killed (wall cap only).
- F2 (E4): the projected-RSS defaults from 4c are carried over; MemoryPeak data comes once 4c's scopes run.
- F3 (E7): whether ending a session stops the process group or the cgroup; one manual outlive check before E7a merges.
- F4 (E10): which harnesses report token usage, and in which line format; one real run per harness before its parser is enabled, otherwise `estimate`.
- F5 (E2, D-2843): the max turn latency per leg. Measured for opencode legs from `opencode.db` (gaps between consecutive `session_message.time_created` in sessions of the last 3 days, coding.vm, 2026-10-25; a gap covers model turn + tool time):

  | model | gaps | p50 | p90 | p99 | max | > 15 min |
  |---|---|---|---|---|---|---|
  | mimo-v2.6-pro | 1966 | 10 s | 120 s | 546 s | 2622 s | 4 |
  | mimo-v2.6-flash | 146 | 19 s | 151 s | 454 s | 618 s | 0 |
  | deepseek-v4.1-flash-free | 260 | 5 s | 42 s | 222 s | 419 s | 0 |

  glm-5.3-flash-free's max (172671 s) is a resumed session, not a turn. Its p99 is 448 s.

  Still open: qodercli (qwen) and agy (gemini), which keep no opencode.db. Measure them from their own session stores, or from the 4a `stdout.log` timestamps once E2's first runs exist. Until then they use the 45 min default.
