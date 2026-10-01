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
