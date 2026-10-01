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

## D-217 (operator via routing-00): no waiting for free quotas. Writers deepseek; seats muse-spark + in-session Sonnet subagent.
If muse fails > 1 h: proceed with Sonnet seat + Sonnet final + routing-00 spot check, noted "seat-2 waiver (D-217)". Keep M2a moving; tell routing-00 when fixes, re-reviews, throwaway round trip are done.

## D-228 hierarchy v4 / D-230 names / D-232 (routing-00 relays; operator decisions) — 2026-10-01 late
- Layers: L0 router, L1 project orchestrator, L2 phase orchestrator, L3 task group = writer + reviewer(s). Use these names in keeper docs/roles. Every L1 runs on OPUS high effort (D-232): my recycle relaunches me on Opus.
- Reviews are RISK-TIERED (replaces "always two", supersedes D-219 except for high tier): per item `review_tier` standard (ONE reviewer, family != writer's) or high (TWO reviewers of distinct families; waiver only here). High = security, secrets, gateway/OmniRoute, deploys, the keeper itself. Router/L1 sets at definition; only router may lower. Default standard except title/track keywords. Import sets high for T0-PAID, T0-KEYS, T4-FENCE, T6-SECRETS, T6-INFRA, T6-UPDATES, T1-* lanes, PG-*. Script ready: $CLAUDE_JOB_DIR/tmp/add_tier.py (text edit of data/plan-v3 after importer supports review_tier).
- ACCEPTED defaults (routing-00): (1) tier `none` only when evidence_kinds==[decision], router verb close_on_decision(key, batch_ref = batch file path + sha256 of docs/plan/graph-updates-2026-10-01.md, verified by the keeper); if the D-row has any `Closes: <project:ID>` the key must be listed; no Closes line => allowed only with the batch_ref. (2) legacy evidence kind `commit` (exists + ancestor of main of registered repo; no trailer/date/CI) router-only via add_evidence_router; repos config only server=/home/s/code/Server, autoos=/home/s/code/AutoOS. (3) router verb set_legacy_state(key,state,note) open->claimed|in_review only, never done, flagged legacy.
- Decision verifier reads /home/s/code/routing/docs/decisions-log.md (NOT DECISIONS.md). Waiver rule: D-row must contain `Waives: <project:ID>` as whole token.
- Router update batch: /home/s/code/routing/docs/plan/graph-updates-2026-10-01.md -> convert (A state changes, B 19 new items, splits T6-UPDATES/T6-GRAPHMOVE/T5-GROK: original cancelled reason 'split', dependents re-pointed, GOAL dep -> IMPL half) into data/updates/2026-10-01-router.yaml, validate, send routing-00 diff + `pg ready --all --brief`, load as router batch ONLY after routing-00's OK. T7-BOARD: after load cancel (reason "operator 2026-10-01: plan graph replaces the board; no kanban"), then edit GOAL deps (drop T7-BOARD) and T7-CONSOLE (plan-graph page is its board view). Order matters: cancel first, then edit GOAL.
- Pilot keeper on coding.vm labelled "isolation not effective (pilot)"; router token to /home/s/.config/plangraph/router.token (0600, never printed; CLI reads file). After load: `pg render --cdn` -> /home/s/code/plangraph/out/plan-cdn.html and tell routing-00. THEN write handoff and reply "handoff ready <path>".
## D-231 scope extension (operator): plan graph = MAIN operator dashboard
Model project clusters -> phases -> tasks; open questions, decisions, blocking operator tasks are first-class items that block dependents. Web GUI = everything an agent can do via keeper API (answer/approve/decide, add/edit tasks, deps/priority, cancel/correct) as the operator principal behind Authelia OIDC, CSRF-safe, every action an attributed event, DONE stays locked. Insight: models per task/phase/project; cost (gateway usage ledger needs OS-41; Claude usage), time, throughput, review outcomes. QUESTIONS.md + decision log migrate in after adoption.
ORDER: finish M2a path first (X6b -> X7 tiers/none/commit/legacy-state -> 2-seat re-review -> pilot keeper -> plan-v3 load -> router batch -> render) THEN best-practice research via cited researcher subagents and write the M2c proposal (data model, GUI write path+auth, stats sources, migration, test plan, cost/time). Router judges; operator approves at milestone. M2b (off-host keeper) is a prerequisite for the GUI write path in production.
## Process learnings: deepseek writers die at ~128k context -> tasks must be small with explicit read lists; never move keeper-code master while children run (R-coord-01); free OR pins unservable (use deepseek); reviews: seat1 = Sonnet subagent (resume via SendMessage a3c1634f6f6e0a97d is session-local, will be lost on recycle -> spawn fresh), seat2 = muse-spark `omniroute/spark-1.3-contributor` pinned via spawn.
