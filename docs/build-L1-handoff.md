CARD: /home/s/code/plangraph-worktrees/build-L1/status/build-L1.card.md (current state; read first)
# build-L1 handoff #6 (gen-6 -> gen-7, 2026-10-16 06:2xZ, ctx cap 230k)
Read first: brief /home/s/code/routing-worktrees/rethink-setup/docs/plan/rethink/build-L1-brief.md, card above.
Startup: skill unattended-orchestration (+ references/main-orchestrator.md); renew claimed leases FIRST (`ops/pg.sh heartbeat plangraph:<KEY>`).
Rules: OPERATOR ALLOWED CRON for build-L1 2026-10-16 (overrides D-1377; memory cron-beat-allowed): create a CronCreate beat `7-59/10 * * * *`
(renew leases from card Open table, check build-L2-*, act on MERGE-READY) right after startup — gen-6 job dies with this session.
D-1364 msgs only BLOCKED/DONE/MERGE-READY/GO/incident/launch. D-1390 effort medium, L2-LAUNCH "sonnet medium". Output caveman. Card <= 40 lines.

## Gen-6 landed (all DONE in keeper)
RECONCILE-SCHED (#21 cf9fc7b), WORKER-PROJ-V1 (#20 69a47cd), TEMPORAL-SPIKE-TLS (#22 bae9b82; gRPC round trip = TEMPORAL-CHECK manual),
KEEPER-CI-CONCLUSIONS (#24 1b923e5; judge server-L1-routing GO; live keeper patched f3bac63 = 7e5a61e + PR #24 diff only, restarted 06:05Z).
D-1402: keeper.json ci += autoos (backup keeper.json.bak-d1402). autoos:CI-WORKST-RUNNER evidence now verified, in_review.

## In flight (claimed by build-L1)
- KEEPER-LIVE-SYNC (HIGH): L2 build-L2-live-sync (coding.vm, own worktree plangraph-worktrees/keeper-live-sync, branch lane/keeper-live-sync).
  Doc docs/plan/rethink/live-sync-1b923e5.md@d8d1bb0: T=1b923e5, migration idempotent in open_db, 10 tables counts equal, no new required key.
  Re-seat ordered 06:1xZ (both seats were fix on bfdfa6d, L2 self-adjudicated = not valid) -> wait SYNC-READY with seats/live-sync-d8d1bb0
  (seat_verdict dry-run rc 0 both, writer anthropic). Then: router GO <sha> restart (ask routing) -> run doc §4 runbook YOURSELF:
  stop unit -> sqlite .backup keeper.db + copy keeper.json + plan.snapshot.json(.sig) -> point live tree at T (ff worktree-proposal to T in a
  separate worktree first, per memory live-tree-merge-safety) -> start -> verify (admin verify, counts, /inbox 200). Abort = restore backup + f3bac63.
- PROJECTOR-LIVE-NAMESPACE-FLAKE: L2 build-L2-ns-flake (workst via workst-L1-autoos, session_01LvCKXyNf3AMojMN3mw8tei); brief
  briefs/l2-live-namespace-flake.md; expects MERGE-READY + 20-run loop log + 1 seat.
- Next after LIVE-SYNC: PROJECTOR-ALARMS-READER (p3, open, unclaimed). Still: CI-RUNS-ON-WORKST (after CI-WORKST-RUNNER done),
  AO-DEEPINFRA-REMOVE (after fixes-L1 priv-writers merge; brief briefs/l2-deepinfra-remove.md).

## Judge protocol (worked all gen)
MERGE-READY -> CI green on PR head + head trailer -> seats as orphan branch seats/<lane>-<sha> in seat_verdict format (review.txt raw byte-exact,
evidence.json: served_model, finish_reason, test_gaming, prompt_sha256, corr; qwen via ops/qseat.py from qodercli -o json) -> extract to
durable plangraph-worktrees/<lane>-seats -> dry-run `python3 <(git show origin/main:tools/seat_verdict.py) --key K --seat-dir D --writer-family F
--token-dir ~/.config/plangraph --dry-run` -> spot-check diff -> `gh pr ready N && gh pr merge N --merge --match-head-commit SHA` (never squash)
-> `ops/pg.sh evidence plangraph:K --kind commit+ci --commit HEAD` -> submit -> verdicts. Openai seat locally: `sh ops/seat.sh ovh/gpt-oss-120b PROMPT OUT TITLE`.
Traps: L2 claims drift from code (TLS "marker planted" was false) -> verify claims in code; a seat "fix" is fixed or judge-adjudicated, never L2-adjudicated;
live keeper tree != main (patch PR diff only unless LIVE-SYNC done); `ops/pg.sh define` refuses unreachable_goal -> router creates items.
Incidents open: mimo-v2.6-flash 402 (writers fall to muse-spark free); live-Temporal "Namespace default" flake (item above).
