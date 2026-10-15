# build-L1 handoff #3 — 2026-10-15 ~19:30Z (gen-3 e37141, context cap)

Read first: brief `/home/s/code/routing-worktrees/rethink-setup/docs/plan/rethink/build-L1-brief.md`, design (binding, 14a)
`/home/s/code/routing-worktrees/rethink-setup/docs/reports/2026-10-15-rethink-setup-design.md`, card `status/build-L1.card.md`, this file,
operator answers `docs/plan/rethink/build-operator-rounds.md` (all of today's are binding). Control branch = `build-L1` (never merged).
Startup: skill unattended-orchestration + main-orchestrator.md, SKILL-READ reply, CronCreate beat (17,47 — fleet rule, routing-00 2026-10-15), build-L1-ALIVE to routing.

## Tools (ops/ on this branch)
- `ops/pg.sh` keeper CLI (token build-L1). Verdicts: `python3 <(git show origin/main:tools/seat_verdict.py) --key K --seat-dir D
  --writer-family anthropic --token-dir ~/.config/plangraph` (needs review-call evidence.json).
- Seats: openai `ops/seat.sh ovh/gpt-oss-120b PROMPT OUTDIR TITLE` (works, ~2-3 min); qwen via qodercli ONLY (operator): empty dir,
  prompt first line "You have NO tools and no repository access. The full material is inline below. Answer in plain text; never emit
  tool calls.", `timeout 900 qodercli -p --model Qwen3.8-Flash --tools "" --no-session-persistence -o json "$(cat P)"` (2 min, pass).
  NEVER ovh Qwen. mimo: `mimo/mimo-v2.6-flash` answers small prompts (15 s); 22 KB seat timed out at 300 s; retry at --max-tokens 6000
  was running (out dir /home/s/.claude/jobs/92200c88/tmp/wc/mimo8b). Always append ops/tail.md to seat prompts.
- Evidence trap: a re-claim bumps the epoch; commits before the claim -> "predates the claim" -> need a post-claim trailer commit.

## Items
| Key | State | Next |
|---|---|---|
| RT-P1-WORKER-CORE, PG-BACKUP-RETENTION | DONE | — (prune GO D-1349 done, -667 MB) |
| RT-P1-TEMPORAL-SPIKE | closed by routing D-1350 | — |
| RT-P1-KEEPER-PG-CODE | claimed, lease 22:25Z (HEARTBEAT before!) | L2 build-L2-keeper-pg: W2 pushed (PR #8 head 4eebb82e, CI running). Agreed option A: CI green -> 1 non-Claude seat on W1+W2 -> I mark #8 ready + merge; W3 new PR from main; final HIGH seats (2 non-Claude) on W1+W2+W3; then commit+ci evidence (post-claim trailer commit), submit, 2 verdicts. |
| CI-PARALLEL | claimed, lease 01:06Z | L2 build-L2-backup (brief briefs/l2-ci-parallel.md) |
| RT-P1-WORKER-CODE | claimed, lease 01:07Z | L2 build-L2-worker-code on the WORKSTATION (bridge session_01HYhk6BmNF9o65Z6McuQxJE, Windows side); writers via workst-L1-autoos harness/opencode/qodercli; I run its gpt-oss seats on request |
| RT-P1-REVIEW-CODE | claimed, lease 01:07Z | L2 build-L2-review-code a5ce98ef (coding.vm) |
| RT-P1-PROJECTOR/PLANNER-KIT/INBOX-CODE | open (b04) | claim + L2 each once KEEPER-PG-CODE W2 is on main (PR #8 merged); briefs follow l2-rules-common.md |
| plangraph:CI-RUNS-ON-WORKST | open (b03r2) | after autoos:CI-WORKST-RUNNER (owner workst-L1-autoos, live under GO) |

## Operator decisions today (verbatim in build-operator-rounds.md)
qwen seats via qodercli; workstation = default CI runner (Docker Desktop, no CPU cap, 32 GB RAM), no GitHub-hosted runners once it
exists; L2 cap lifted (floors coding.vm 1 GB, workstation 12 GB free); writers free first (mimo -> muse-spark free -> OpenRouter free ->
qodercli; Sonnet last, D-1351); private-safe guard dropped for writers on all private repos (D-1358, fixes-L1 implements);
workstation launches via workst-L1-autoos (name, cwd C:\Users\mauls\Documents\Code\plangraph, model, prompt). Production trials only
after router judge + operator GO.

## L2 protocol
Briefs: docs/plan/rethink/briefs/ (l2-rules-common.md binding). L2 report -> judge (CI, seat evidence, spot-check code) -> merge PR ->
evidence -> submit -> seat_verdict -> `L2-DONE <name>` to routing. Spawner admission floor 4608 MB still blocks L3 on coding.vm until
fixes-L1 applies D-1352 -> L2s fall back to in-session Sonnet (recorded).

## Research
Free CI runners report: docs/plan/rethink/research/free-ci-runners.md (sent to routing). Local suite coding.vm 960 s at 6% CPU vs CI 213 s.
