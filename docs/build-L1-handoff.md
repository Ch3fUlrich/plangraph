# build-L1 handoff #2 — 2026-10-15 ~16:15Z (gen-2 8f5ea8, context cap)

Read first: brief `/home/s/code/routing-worktrees/rethink-setup/docs/plan/rethink/build-L1-brief.md`, design (binding, 14a)
`.../docs/reports/2026-10-15-rethink-setup-design.md`, card `status/build-L1.card.md` (this branch), then this file.
Control branch = `build-L1` (this worktree); never merged to main. Code lanes = Sonnet subagents in worktrees under
/home/s/code/plangraph-worktrees/ (from origin/main); push lane/*, PR, I merge after CI + seat.

## Tools (this branch, ops/) — copy to your job tmp and fix paths
- `ops/pg.sh` keeper CLI (token build-L1). `ops/probe.sh LABEL` live-keeper probes (health, verdict bool on a NONEXISTENT key, router whoami).
- `ops/seat.sh MODEL PROMPT OUTDIR TITLE` = review-call (300 s hard cap, default max_tokens 16000; call review-call directly for --max-tokens).
- `ops/assemble.py OUT part|label=path...`; ALWAYS append `ops/tail.md` (answer-shape reminder) as the last part, else gpt-oss
  answers only the standing question, bolds the VERDICT, or ends TEST-GAMING without ": 0 hits" (-> test_gaming=missing).
- Record verdicts with `tools/seat_verdict.py` (on main since PR #4): `--key K --seat-dir DIR --writer-family anthropic
  --token-dir ~/.config/plangraph [--dry-run]`. PASS needs finish_reason=stop AND test_gaming=no. Reviewer tokens: openai, meta,
  xiaomi, deepseek, alibaba (issued 16:10 by fixes-L1).
- Seat models now: `ovh/gpt-oss-120b` works (flaky format, sometimes hallucinates evidence -> adjudicate against code).
  xiaomi-mimo flash+pro: every call timed out since ~15:40. `ovh/Qwen3.8-27B` (exact case! alibaba, D-1329 2nd family):
  timed out at 16000 max tokens; at --max-tokens 5000 finish_reason=length with EMPTY answer (reasoning eats budget) -> try ~10000 or a smaller prompt.
- Batch replay: ops/mkreplay.py (edit T path), daemon from a lane worktree's src on :18787, apply_batch.py with router token, kill + rm.

## Lessons (new)
- Plan-Item trailer must be in the LAST paragraph together with Co-Authored-By (no blank line), else git ignores it and
  commit+ci evidence fails ("does not carry trailer"). Check `git log -1 --format='%(trailers:key=Plan-Item,valueonly)'`.
- D-1338 (operator): every red CI run emails the operator. Local suite green before every push. CI now runs on push only for
  main; lanes need a (draft) PR for CI; concurrency cancels superseded lane runs (PR #7, main ab3d0817).
- Public scrub test (tests/test_split_make_public.py) rejects token-shaped test constants.

## Items (owner build-L1)
| Key | State | Next |
|---|---|---|
| plangraph:RT-P1-CONTRACT | DONE (main 7e5a61e3, live via GO D-1331, keeper "ci" block live) | — |
| plangraph:RT-P1-TEMPORAL-SPIKE | in_review, tier none | routing closes |
| plangraph:RT-P1-WORKER-CORE | claimed, lease 18:13Z (heartbeat!) | commit+ci✓ (fad33f3c). r2 lane lane/rt-p1-worker-core-r2 291617ff, PR #6, CI green. Seats: r5 fix -> triaged (2 confirmed fixed, 19 false), r6 PASS (test_gaming missing), r7 fix = hallucinated (adjudication docs/plan/rethink/seats/worker-core-r2-adjudication.md). Need 1 recordable seat: qwen 5000-token try failed (length, empty); next: ovh/gpt-oss-120b on prompt wc/r6.md (copy from /home/s/.claude/jobs/abc4dacf/tmp/wc/) or qwen ~10000. Then merge PR #6, commit+ci on 291617ff (trailer ok), submit, seat_verdict. |
| plangraph:RT-P1-KEEPER-PG-CODE | claimed (b02 applied, D-1334), lease 18:13Z | W1 writer (Sonnet subagent of gen-2) running: brief docs/plan/rethink/briefs/keeper-pg-code-w1.md, worktree /home/s/code/plangraph-worktrees/keeper-pg, branch lane/rt-p1-keeper-pg-code. If the subagent died with gen-2: inspect the worktree, relaunch with the same brief + D-1338 rules (PG job continue-on-error until parity, draft PR for CI). Then W2 (design-3 tables/states), W3 (copy tool). HIGH: 2 families (openai + alibaba). |
| plangraph:RT-P1-REVIEW | open (dep WORKER) | seat_verdict tool already on main (trailer commit a28d0119) |

## Carry into RT-P1-WORKER (from worker-core reviews)
fresh worktree on promotion (design 4.5 l.126); symlink escape needs harness sandbox; 24 h wait cap -> park; probe_fail attempt cap.

## Other state
- All other P1 items gate on server:RT-P1-PG-DEPLOY (server-L1-main / server-L2-rtp1-pg-code).
- Backups from D-1331: ~/.local/share/plangraph/keeper.{json,db}.bak-20261015-contract (keep until KEEPER-PG cutover).
- Removable worktrees: /home/s/code/plangraph-worktrees/{rt-evidence-1,ci-triggers}, .claude/worktrees/agent-{a3aff9b8a3414d80b,aef4af91b3ec7281e,aaf50bb78352c3468,af32cede7c100d08a} (lanes merged).

## Update 16:55Z — KEEPER-PG-CODE W1 finished
Head 41bb2307 on lane/rt-p1-keeper-pg-code, draft PR #8; CI green both jobs incl. pytest-postgres (runs 38770815285 push, 38770823100 PR; no continue-on-error).
Backend chosen only by keeper.json `backend: postgres` / PG_KEEPER_BACKEND; advisory write lock; schema_pg.py migrations + `pg admin migrate`;
parity test tests/test_keeper_pg_parity.py (event rows, chain head, item state). Plan + open questions: docs/keeper-postgres.md.
Open: 5 SQLite-file tests deselected on PG (3 frozen contract tests in test_events.py — needs a judge note or an HTTP-level replacement);
lane branch predates PR #7 (merge origin/main to pick up trigger change). Next: merge origin/main, W1 HIGH seats (gpt-oss + qwen/alibaba,
split by module, tail.md), then W2 (design-3 tables/states) + W3 (SQLite->PG copy tool). Commit+ci evidence needs a lane commit with trailer.

## Gen-3 (e37141) state 19:10Z
- DONE: RT-P1-WORKER-CORE (main dd6300cf; commit+ci 53e7f78e; verdict openai), PG-BACKUP-RETENTION (PR #10 6f644e71; live prune GO D-1349:
  du 1644093178 -> 976613114, log docs/plan/rethink/go/prune-d1349.log), TEMPORAL-SPIKE closed by routing (D-1350).
- Claimed (lease ~01:07Z / KEEPER-PG-CODE 22:25Z): RT-P1-KEEPER-PG-CODE, CI-PARALLEL, RT-P1-WORKER-CODE, RT-P1-REVIEW-CODE.
- L2s: build-L2-keeper-pg (787f3984; W2 early PR, W3, seats), build-L2-backup (4c1ebfe8; now CI-PARALLEL, brief briefs/l2-ci-parallel.md),
  requested: build-L2-worker-code (workstation), build-L2-review-code. Common rules: briefs/l2-rules-common.md.
- Batches live: b03r2 (CI-PARALLEL, autoos:CI-WORKST-RUNNER owner workst-L1-autoos, CI-RUNS-ON-WORKST), b04 (WORKER/REVIEW/PROJECTOR/
  PLANNER-KIT/INBOX -CODE). PROJECTOR/PLANNER-KIT/INBOX CODE start after KEEPER-PG-CODE W2 is on main (claim then, L2 each).
- Operator today (verbatim in docs/plan/rethink/build-operator-rounds.md): qwen seats via qodercli, never ovh Qwen; workstation =
  default CI runner (Docker Desktop, no CPU cap, 32 GB), no GitHub-hosted runners once it exists; L2 cap lifted, floors coding.vm 1 GB /
  workstation 12 GB; writers free first (mimo-v2.6-flash -> muse-spark free -> OpenRouter free -> qodercli; Sonnet last).
- Seat recipe qodercli: empty dir, prompt prefixed "You have NO tools ...", `qodercli -p --model Qwen3.8-Flash --tools "" -o json`.
- Open: free-CI research subagent -> docs/plan/rethink/research/free-ci-runners.md (routing asked for it, D-1349 (3)).
- Local suite on coding.vm 960 s at 6% CPU (tests wait) vs CI 213 s.
