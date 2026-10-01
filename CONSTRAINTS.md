# Pinned constraints — plangraph-L1-main (re-read after any compaction)

Source: /home/s/code/routing/docs/plan/briefs/plangraph-L1-main.md. Talk to routing-00 ONLY (SendMessage `routing`).

## Requirements (operator, 2026-10-01)
1. Agents first: read/write/decide by small queries (ready, blocked-by, parallel, owner), never big prose plans.
2. DONE is immutable (prevention, not detection); correction = new item referencing old; every change attributed
   (agent, model, time); DONE needs verifiable evidence (commit sha, CI run).
3. Human visualization: start -> parallel branches -> goal, state colours, click details, per-project filter, phone usable + ready/blocked list.
4. Multi-project; routing-00 orchestrates every L1 from the graph's ready set.
5a. (later) Autonomy: each project's L1 works FROM the graph alone (pull ready, claim, delegate, evidence, move on), escalates
   only on problems; routing-00 orchestrates via the graph and JUDGES my proposal/page/capabilities before the operator.
   Operator reviews at M1 proposal, M2 pilot running, M3 test scorecard, M4 adoption only.
5. Check thoroughly that no existing app does this; prefer maintained OSS, self-hosted, API/MCP; build only the glue.

## Work order
1 Proposal (one page, in repo; options each one plain sentence; req 1-5 fit; recommendation; pilot; tests; cost/time) -> path to routing-00; operator chooses.
2 Pilot after choice: import plan v3; validator (no cycles, all reach goal, DONE needs evidence, DONE locked); views (ready, blocked-by, waves, critical path, % done); human page; CLI/MCP (ready, show, claim, set_state w/ evidence); routing hook.
3 Tests (TESTING.md style): immutability attacks, 3 concurrent claims, token cost vs file reads, desktop+phone screenshots, import round-trip, failure drill.
4 Adoption report -> routing-00.

## Rules
- Orchestrate; L2/L3 do work via autoos-agent MCP (trial -> free -> credits; Sonnet finals). Web research via Sonnet/Haiku subagents, every claim sourced.
- Reviews: two seats of distinct families, resolved model recorded (D-206) + Sonnet final. Local-green before push; no PR while CI red (D-155).
- Heavy runs (installs, DB servers, browser/screenshot, builds, load) -> workstation via routing-00. If coding.vm MemAvailable < 4 GB, start nothing new locally.
- Plan data = site data: private repos only. Never read/print secrets. No gateway live steps, no deploys, no changes in other repos (read only). Files <= 400 lines.
- Every DONE note records the model. Claims need evidence.
- Questions for operator go through routing-00 with options + default. Milestone reports short, with paths.
- Keep status/plangraph-L1-main.card.md (<=40 lines) current. No remote without asking routing-00.

## routing-00 acceptance criteria for the proposal
- Req 1-5a each -> concrete mechanism, PREVENTED vs only DETECTED.
- Immutability by prevention on every path agents hold (CLI, MCP, SQL, files); corrections = new linked items; identity from auth, not self-asserted.
- One writer of definitions (router/L1 per ownership table); workers claim/evidence/verdict only; explicit state machine; leases with expiry.
- L1 autonomy loop: exact command sequence + escalation triggers.
- Token cost per agent question MEASURED; phone page; multi-project + cross-project edges; store-down behaviour; dependency maintenance risk (churn, licence).
- Least new infrastructure; reuse before build.

## Facts routing-00 already holds (do not restate)
Beads/Dolt can reopen/update/delete/sql-edit closed items, actor self-asserted; Gas Town owns the runtime (overlap); no self-hosted PM tool draws a dependency network.
Working in place on the empty repo is fine until the first commit; worktrees after that. Round-2 tool-sweep file still pending (routing-00 will ping).

## M1 verdict (routing-00, 2026-10-01): APPROVE C over B (operator confirms architecture shortly). Conditions:
1 PLACEMENT: coding.vm agents run as user `s` (docker+lxd groups = root-equivalent) -> 0700 DB isolation void there. Production keeper on a
  host where agents have no shell (small VM/LXC deployed by prox via Semaphore), API over tailnet only. M2 pilot on coding.vm labelled
  "isolation not effective". Test 1b runs against the off-host keeper and checks docker/lxd/sudo group membership.
2 STAGE: M2a = store+triggers+state machine+whole-graph validator+views+evidence verifier+CLI/MCP+import+page, simple per-principal tokens.
  M2b = run-bound tokens, signed snapshot, outbox replay, hash-chain anchor. Report REAL line counts (router expects >1,500).
3 EVIDENCE KINDS pluggable: commit+ci, run-record, semaphore-task (id, template, status success), decision (D-id exists in routing/DECISIONS.md).
4 IMPORT: plan-v3.import.yaml goes to routing-00 for review BEFORE import; router owns plan content; definitions router-attributed.
5 PAGE: `pg render` = one static HTML; routing-00 publishes it as private artifact; vendored JS, no external fetch beyond cdnjs/jsDelivr.
6 REVIEWS: merges need D-206 seats with model_source pinned/resolved; pin distinct free models per seat; unresolved families don't count.
Start now (same for B and C): import YAML draft, views module, page. Keeper/store starts when operator confirms C.
Remote: none until M2. Postgres = M4 decision. Work lives on branch worktree-proposal (worktree .claude/worktrees/proposal).

## D-212 (routing-00 relay, 2026-10-01): paid deepseek-v4.1-flash and muse-spark-1.3-contributor allowed for writers/review seats when free legs are rate-limited
Code carries no site data. Suggested: writers on deepseek (pinned, resolved); seat 1 = muse-spark; seat 2 = in-session Sonnet subagent. Seats valid only with resolved models. Log paid spend per run.
If a spawn fails on an unservable leg, use a pinned free model for that try and tell routing-00. Any refusal: stop, report exact text. Never route around a denial.
Review scheme (earlier msg): seat 1 Sonnet subagent (Agent tool), seat 2 one pinned model of a third family via autoos-agent spawn only; routing-00 does an Opus spot review at end of M2a.
Disk on coding.vm ~99%: keep out/ small, nothing heavy until routing-00 says >=5 GB free.
