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
| (1) | `route` returns `direct_edit` when `est_lines <= 30` and no path is a door or secret; otherwise a writer leg | E3 |
| (2) | `ci_commit_gate` allows a `Hand-Edit` of up to 30 lines outside `AUTH_DOOR_PATHS` | E1 |
| (3) | the spawner kills a run with no worktree write and < 2% CPU for 15 min (exit class `stall`, liveness `stalled`, slot freed, recorded) | E2 = PG-EFFICIENCY-GATES 4b, amended below |
| (4) | the host gate keeps at least 1 slot for every project with queued work; seats are counted apart from writers | E4 |
| (5) | the seat parsers accept the "LOW only" variants and post a seat that has a D-id refutation file as pass-with-lows (3.4) | E5 |

## Slices

| slice | scope | max_lines |
|---|---|---|
| 1 | E1 hand-edit cap 30 + shared door list (crit 2) | 120 |
| 2 | E2 = PG-EFFICIENCY-GATES slice 4b with the writer/seat stall split below (crit 3); Plan-Slice stays `PG-EFFICIENCY-GATES.md#5`, this row only books the order | 300 |
| 3 | E3 route `direct_edit` + spawn refusal (crit 1) | 300 |
| 4 | E4 host admission (row moved from PG-EFFICIENCY-GATES 4c) + per-project slot reservation + seat slots (crit 4) | 400 |
| 5 | E5 seat parsers: LOW-only variants + D-id refutation file (crit 5) | 300 |

Build order with slice 4 (one task loop, routing D-2837):

4a (written, `a43a7ff8`) -> E1 -> E2 (4b) -> E3 -> 4a2 -> 4a3 -> E4 -> 4c (scope and OOM) -> E5 -> phase review.

- E1 and E2 land first, as the item asks.
- E2 and E4 are the same rows as PG-EFFICIENCY-GATES 4b and 4c's host admission (D-2837): their commits carry two Plan-Slice trailers, `docs/design/PG-EFFICIENCY-GATES.md#5` (resp. `#6`) and `docs/design/ORCH-EFFICIENCY-ENFORCE.md#2` (resp. `#4`), so both items close on the same evidence.
- E1 touches the gate path `tools/ci_commit_gate.py`, so it is a spawned writer with `Gate-Edit: D-2835`, never a Hand-Edit.
- E2 needs only 4a: without 4a2, a stalled run ends `failed` with no fall-through, which still meets crit (3).
- E5 has no dependency. It may run in parallel once lane/review-verdict-parse (the build-L1 mimo-pro writer for the 6ef7b09d spec) has merged, because E5 builds on its parser.

### E1 hand-edit cap (crit 2)

| piece | where | behaviour |
|---|---|---|
| cap | `tools/ci_commit_gate.py` `HAND_EDIT_MAX_LINES = 30` | added + deleted lines per Hand-Edit commit, measured as today (`_changed_lines`, numstat; binary = refused as today); the door check and the malformed-trailer check are unchanged |
| shared door list | new `src/plangraph/gates/doors.py`: `AUTH_DOOR_PATHS` (same tuple), `HAND_EDIT_MAX_LINES = 30`, `door_path(path) -> bool` (fnmatchcase, the same semantics as the gate), `blocked_path(path) -> str or None` (`"door"` or `"secret"` via `gates.privacy.secret_path`) | `ci_commit_gate.py` stays self-contained, because CI runs the BASE copy from `$RUNNER_TEMP` (`.github/workflows` step "ci_commit_gate_base.py"), so it cannot import `src`. A drift test asserts `ci_commit_gate.AUTH_DOOR_PATHS == doors.AUTH_DOOR_PATHS` and that the two caps are equal |
| bootstrap | Gate-Edit trailer `Gate-Edit: D-2835` (`GATE_PATHS` includes `tools/ci_commit_gate.py`) | the BASE copy judges the commit that raises the cap, so that commit must be a writer commit (Writer trailer), never a Hand-Edit; the new cap applies to the commits after it merges |

E1 tests: Hand-Edits of 30 lines (pass), 31 lines (refused), 30 lines touching `tools/seat_verdict.py` (refused, door), and a secret-shaped path; the drift test; the existing gate suite stays green.

### E2 stall kill = 4b, amended (crit 3)

4b ships as designed: polling monitor, injected `sleep`/`clock`/`tick`, pure `liveness.check`, the waiting exemption, and the content-hash loop rule. The kill is `runner.cancel(job, liveness="stalled")`, which ends the run with `exit_class=stall`, frees the process and its slot (no runner job left `running`), and records `liveness`/`exit_class` in the record and in `result.json` (4a). The amendment splits the rule by role, because crit (3) names worktree writes and a seat has none.

| role class (4a `spawn_limits` map) | stalled when, for `stall_min` (15) | why |
|---|---|---|
| writer (`implement`, `fix`, `writer`) | no new commit on the run branch AND no file under cwd with new content (hash rule) AND process-group CPU < `stall_cpu_pct` (2) | crit (3) as written. New stdout bytes no longer count as work for writers: run-ae0dcb3c streamed nothing useful for 58 min. A writer that thinks for 15 min without writing a file is stalled by design |
| seat (`review`) | no new stdout/stderr bytes AND CPU < 2% | a seat writes no files. Whether qodercli `-p`, `opencode run` and agy `-p` stream output before they end is NOT verified (open fact F1 below), so the seat rule ships only for harnesses listed in `spawn_limits.streams` (default empty); every other seat is bounded by its 45 min wall cap (4a2) alone |
| default | as writer | |

- The waiting exemption applies to every role: a recorded provider wait counts as `waiting` until its reset + 5 min.
- `spawn_limits` gains `streams` (a list of harness names; validation refuses any other shape), following the 4a reader rules.

Open fact F1, verified before `streams` gets any entry: run each seat harness once through the spawner on a long prompt and check that `stdout.log` (4a) grows before the exit. Until that run, no seat is stall-killed.

E2 tests add to 4b's:
- a writer streaming output with no writes, 0.5% CPU, 15 min -> stalled (4b's "streaming, no writes = alive" case flips for writers);
- a seat on a non-streaming harness, silent for 40 min -> alive until the cap;
- a seat on a harness in `streams`, silent with low CPU -> stalled;
- after a kill, the runner holds no running job and `result.json` has `exit_class=stall`, `liveness=stalled`.

### E3 route `direct_edit` (crit 1, R2)

| piece | where | behaviour |
|---|---|---|
| decision | pure `gates.direct_edit.decide(card, role, limits) -> (mode, reason)`, called at the end of `SpawnService._route` | `mode = "direct_edit"` only when ALL hold: `role == "implement"`; the card is not a fix card (`admission.is_fix` false: fix rounds keep the D-2739 0.2d writer path); `card.est_lines` is an int, 1..`direct_edit_max_lines` (30); `card.paths` is a non-empty list of repo-relative strings, none `doors.blocked_path` (door or secret); the card is not `privacy=sensitive`; the card has no `lane_class` in `{"auth", "access", "secrets"}` (3.13: those lanes always go to a spawned writer). Anything else, a missing or malformed field included, -> `"writer"` (the safe, current behaviour; reason names the failed condition) |
| plan field | `SpawnPlan.mode` (`"writer"` default) + `mode_reason`; `to_dict` includes both | `route --json` shows them; legs are still listed (they are the fallback if the orchestrator overrides) |
| spawn refusal | `SpawnService.spawn` before `_spawn` | `mode == "direct_edit"` -> `SpawnRefused("direct_edit: <= 30 lines outside door paths; make it a Hand-Edit commit (DECISIONS 3.13)")`, stored by `save_refusal` as a record with `refused=direct_edit`; `--override-reason` lets the run proceed, and the record keeps `mode`, `mode_reason` and the override text (attribution, like leg overrides) |
| limits | `spawn_limits` gains `direct_edit_max_lines` (default 30, int 0..30; 0 turns direct edit off) | the cap may never exceed `doors.HAND_EDIT_MAX_LINES`; validation refuses higher values, so route can never send work the gate would refuse |

- Est lines and paths come from the card the orchestrator writes. A card that understates them gets a direct edit that the CI gate (E1) still refuses above 30 lines, so the gate is the hard bound and route is the cost rule.
- A missing `est_lines` routes to a writer and does not refuse: cards written before this item keep working.

E3 tests:
- the decision table: each condition failing alone -> writer with its reason; all holding -> direct_edit;
- `paths` containing `src/plangraph/keeper/x.py`, `.github/workflows/a.yml` or `vault/k.json` -> writer;
- a cap above 30 refused;
- spawn refused without an override, and proceeding with one (record fields checked);
- `route --json` carries `mode`.

### E4 host admission + per-project slots (crit 4)

The host admission row of PG-EFFICIENCY-GATES 4c moves here unchanged: dedup keys, pid + start-time match, client process patterns including agy `-p` (D-2823), projected RSS p90 never below the harness default, `floor_mb` 2000, and the `host_full` refusal at all three launch sites. The additions:

| piece | where | behaviour |
|---|---|---|
| host registry | `~/.local/state/plangraph/host/` (env `PLANGRAPH_HOST_DIR`): `runs/<run>.json {run_id, project, role_class, pid, start_time, host}` written at admission and removed at exit; `queue/<project>/<run>.json {run_id, created, pid, start_time}` | host-wide, shared by every spawner state dir on the host: each L1 passes its own `--state`, so per-state records cannot see the other projects. Stale entries (pid gone, or start time mismatch) are ignored and swept. `project` = the card key prefix (`plangraph:` -> `plangraph`); no key -> `unkeyed` |
| atomic admit | `host_load.admit_host(probe, limits, project, role_class)` holds an exclusive lock on `host/lock` (`fcntl.flock`; `msvcrt.locking` where fcntl is missing, see `tests/test_mcp_no_fcntl.py`) for count + decide + register | two spawners cannot both take the last slot |
| writer and seat slots | `spawn_limits` `max_runs` (4) stays the total; new `max_seat_runs` (2) | seat runs (registry `role_class=seat`) count against `max_seat_runs` and never take a writer slot; writer/default runs count against `max_runs - max_seat_runs` (2 writer slots by default). Client processes found without a registry entry (other tools, raw CLIs) count as unattributed writer-slot users. The memory floor covers all runs together |
| reservation | same function | let `F` = free writer slots, and `W` = the projects other than the caller that have a live queue entry and zero running writer-slot runs. A writer-class run of project P is admitted iff `F >= 1` and (P has zero running writer-slot runs, or `F > len(W)`). Within a project, the oldest queue entry goes first. Seats use the same rule over seat slots |
| queue wait | `spawn` refused for slots (not memory) writes its queue entry and polls `admit_host` every `tick` (injected `sleep`, 30 s) for up to `queue_wait_min` (`spawn_limits`, default 60; 0 = refuse at once, as today); expiry -> `host_full` refusal; the entry is removed on admit, expiry, refusal or exit | "queued work" = a live queue entry, so a project that is waiting is visible to the others; the L1 shell loops (`hostgate.sh`) are no longer needed |

E4 invariants:
- I-E4a: a project with a live queue entry and no running writer gets the next free writer slot before a project that already runs one.
- I-E4b: a seat never takes a writer slot, and a writer never takes a seat slot.
- I-E4c: admission is decided under the host lock (count, decide and register as one step).
- I-E4d: stale registry and queue entries never count.

E4 tests (fake probe, temp host dir, fake clock and sleep):
- 4c's dedup, agy `-p` and pid-reuse cases;
- 2 writer slots held by project A while B is queued -> A's next writer waits and B's is admitted when a slot frees;
- seats admitted while the writer slots are full;
- an unattributed client process takes a writer slot;
- two admit calls racing under the lock -> one admitted;
- queue expiry -> `host_full`;
- stale entries ignored.

### E5 seat parsers (crit 5, DECISIONS 3.4)

| piece | where | behaviour |
|---|---|---|
| LOW-only variants | one rule in `gates/findings.py` `low_only(verdict_line, body) -> bool`, used by both `parse_seat_verdict` and `tools/seat_verdict.parse_verdict` (seat_verdict already imports `src` for `body_blocking`) | a VERDICT line whose first word is a fix word (`fix`, `changes`, `fail`, ...) followed, within the same line, by `LOW only` / `LOWs only` / `only LOW(s)` (case-insensitive, any punctuation between, e.g. "fix - LOW only:") counts as pass-with-lows when the body has no HIGH/MED finding (`blocking(parse_findings)` empty); with a HIGH/MED in the body it stays changes (0.2c); `seat_verdict` posts `ok=true`, reason prefix `pass-with-lows` and the LOW lines; this builds on lane/review-verdict-parse (6ef7b09d spec) and must match its variants |
| refutation file | `tools/seat_verdict.py --refutation <file> --decisions-log <path>` | file format: line 1 `Refutation: D-<digits>`, then one line per blocking finding `<id> <path>:<line> <reason>`. Accepted only when ALL hold: the D-id appears as a row id in `--decisions-log` (the routing `docs/decisions-log.md`; a missing or unreadable log refuses, so an L1 cannot invent a D-id); every blocking id of the seat body has a line; each `path` exists at `--head` (`git cat-file -e <head>:<path>`) and `line` is within its length. Then `ok=true`, reason `pass-with-lows: refuted per D-<n>: <ids>` + the refutation lines. Any failed check -> exit 2 (refused), nothing posted |
| no re-run | unchanged | the seat's own text is never edited, and the refutation is a separate file named in the reason; 3.4's "never re-run a seat with rulings in its prompt" is untouched |

E5 tests:
- "VERDICT: fix - LOW only:" with LOW lines -> pass-with-lows; the same with a MED line -> changes; "VERDICT: FIX" with LOWs and no "only" -> changes (unchanged);
- refutation accepted (D-id in the log, every id covered, path:line valid);
- refutation refused for each of: an unknown D-id, a missing log, an uncovered id, a missing path, a line past the end of the file;
- both parsers agree on a shared fixture table.

## Invariants

| id | invariant |
|---|---|
| IE1 | No Hand-Edit and no `direct_edit` route ever touches `AUTH_DOOR_PATHS` or a secret-shaped path; route's line cap is never above the gate's. |
| IE2 | Route mode is a pure function of (card, role, limits); a missing or malformed input yields `writer`, never `direct_edit`. |
| IE3 | A stall kill always leaves `exit_class=stall`, `liveness=stalled` in the record and `result.json`, and no runner job in `running`. |
| IE4 | Host admission is atomic under one host-wide lock, and stale registry and queue entries never count (I-E4a..d). |
| IE5 | A pass-with-lows post needs either a VERDICT line that says LOW only with no HIGH/MED in the body, or a refutation file whose D-id exists in the routing log and which covers every blocking id with a valid path:line. |
| IE6 | All decisions are pure functions of injected inputs (clock, sleep, probe, git reader, host dir) and are tested hermetically: no real models, systemd, network or memory pressure. |

## Input classes (E3, E5)

| input | class | result |
|---|---|---|
| card without `est_lines` or `paths` | legacy card | writer, reason `missing est_lines/paths` |
| `est_lines` 31, or a non-int | over cap / malformed | writer |
| `paths` with `keeper/`, `.github/`, `seat_verdict.py`, `vault/` | door / secret | writer |
| fix card <= 30 lines | fix round | writer (0.2d) |
| `VERDICT: fix - LOW only:` + LOW lines | LOW only | pass-with-lows |
| `VERDICT: fix - LOW only` + a MED line | contradiction | changes |
| refutation with a D-id that is not in the log | invented ruling | refused |

## Open facts

- F1 (E2): whether the seat harnesses stream stdout before they exit. Until verified, no seat is stall-killed (wall cap only).
- F2 (E4): the projected-RSS defaults from 4c are carried over; MemoryPeak data comes once 4c's scopes run.
