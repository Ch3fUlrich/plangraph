# Handoff: plangraph-L1-plan2 (2026-10-22, updated before clear)

Role: an L1 planning session (Opus) in plangraph. It plans and does not build. Questions go to routing; only L0 asks the operator
(DECISIONS 5.1), unless the operator asks this session directly.
- Rules: terse comms (D-649); every task is a plan-graph item; no crons (D-1377).
- Git: lane branches only, never merge.
- Never read or print token files (D-349).
- AutoOS and the public plangraph parts carry no hostnames, IPs, user names, home paths or secrets.

## State: nothing open or owned by this session

| Work | Where | Status |
|---|---|---|
| Orchestration setup plan (S1-S23), fleet capacity (section 12), L2 briefs | `lane/orch-setup-plan` c4661514 | applied D-1532; briefs follow model order D-1704 / D-1705 (agy code only, D-562) |
| RT-P0 triage (448 items) | `lane/rt-p0-triage` bfd4f8b3 | applied D-1535 |
| Owner reassignment (292) | `lane/owner-reassign` 3fa72727 | applied D-1542 |
| UI plan: `docs/plans/ui/2026-10-17-ui-plan.md`, spec `docs/ui/VIEWS.md`, L2 briefs U1-U5 | `lane/ui-plan` | applied D-1730, D-1731; briefs at 2bd22de4; PH-UI-STABLE under GOAL-PG-PLAN (D-1747) |
| UI overlap scope narrowing (PG-ANSWER-UI, QA-UI, PG-QUESTION-CARDS, PG-DEC-EDIT-NOTICE) | `lane/ui-plan` 66cb434e | applied (keeper texts verified 2026-10-22) |
| UI-STABLE progress (head_seq 9500) | keeper | UI-VERSION-STAMP done; UI-BROWSER-TESTS back to open (no longer claimed); the other 7 lanes and PH-UI-STABLE open |
| Operator answers 2026-10-22 | relayed to routing | Q-BUILD-TOKEN-ROUTING = leave as is; DECIDE-CH-COMICS = wait for the CH-SCOPE-COMICS study; EBN-PROVIDER-PACK = the operator asks the provider; WSL Claude login renewed |
| MT-003 and MT-095 reword | `lane/ui-plan` 036735f3 | applied D-1818; D-1613 path exemption stays (operator) |
| Operator answers relayed | D-1816 (12 answers) | recorded; OP-HEAL-ADOPT waits for its scorecard |

## Open points (not owned here)

- The decisions S1-S23 are not persisted to Omnigraph (this session had no Omnigraph MCP tool). They are durable in the design doc and
  the keeper.
- `lane/orch-setup-plan` holds a local, uncommitted CARD.md edit (status card). Do not push it without routing's OK.
- Operator side: freeing the repo name for SPLIT-PUBLISH.
- Open questions on the operator (head_seq 9500):
  - OP-HEAL-ADOPT waits for the pilot scorecard;
  - Q-SENTRY-TRIAL waits for the eval;
  - DECIDE-CH-COMICS waits for its study;
  - invest:OWN-LIVE is not ready;
  - EBN-PROVIDER-PACK and EBN-DON011187-TRANSFER are with the operator.
- Next for a fresh session: none assigned. Watch UI-BROWSER-TESTS. It went back to open, and U3/U4 wait on it, so if it stays
  unclaimed, tell routing.

## How-to

- Snapshot (WSL): `cd ~/code/plangraph && PYTHONPATH=src python3 -m plangraph.keeper.cli --config ~/.config/plangraph/reader.json sync pull`;
  the file is `~/.local/share/plangraph/plan.snapshot.json`.
- Batch dry-run: `python tools/apply_batch.py <yaml> --token-file <dummy> --dry-run`.
- Batch rules:
  - edits only on open items;
  - every live item must reach a goal-kind item;
  - `evidence_kinds` lists must ALL be present;
  - the credential guard refuses `word/word6+` near credential words (paths with 2 slashes plus an extension are exempt).
- Routing reaches this session over the bridge; reply to its `from` id.
