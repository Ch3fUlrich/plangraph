# Proposal: the plan graph tracks acceptance without hand batches (2026-10-03)

Problem: the graph lags reality (example: Server 234 open / 13 done) because an item only closes when someone writes a close batch or an
ANSWERS line. Router acceptance already happens (a D-row "ACCEPTED ..."), but nothing turns it into graph state.

## Two paths, in order

### Path 1 — now (no keeper change): md-sync honours router acceptance D-rows
- md-sync already reads docs/decisions-log.md and uses the KEEPER's own `Closes:` parser (cf8e6af).
- New rule: for every D-row whose BY column is `router` (or contains `router`) and whose decision cell has a `Closes:` keyword, md-sync
  plans the same close sequence it uses for ANSWERS entries, for every listed key that is live and not done/cancelled:
  `edit tier none + [decision]` (only if needed) -> `legacy_state in_review` (if open/claimed) -> `add_evidence decision D-n` ->
  `close_on_decision`, all inside the one atomic batch; batch_ref = decisions-log.md @ sha256 (already in batch_roots).
- Skips stay visible: a listed key that does not exist, is cancelled, or is DONE already -> `close-skipped` / status file (unchanged
  mechanism). A D-row Closes token never cancels and never edits definitions beyond tier/evidence.
- Guard rails: only router-attributed rows; only keys in the row's own Closes cell (fail-closed parser, whole tokens); idempotent
  (a second run plans nothing); an item closed this way records the D-id as evidence, so the page shows WHY it is done.
- Cost: one writer task on tools/md_sync_plan.py + tests (synthetic), two seats, then live. Effect: every acceptance D-row the router
  already writes closes its lanes within 10 minutes.
- Router habit needed: put `Closes: <project:ID ...>` at the end of every ACCEPTED row (it already does for most).

### Path 2 — target (operating core): L1s drive their own items through the keeper verbs
- At start: L1 `claim <key>` (lease). At hand-off for review: `submit <key>` with evidence (commit sha on main + CI run id, or a
  semaphore task id). Seats/router record `verdict` (pass/changes) with the resolved model families; the keeper closes when the tier's
  verdict rule is met. DONE then carries real evidence instead of a decision reference.
- Needs: run-bound L1 tokens and the MCP remote path (M2b), and the `intent`/acceptance field (MODEL-KINDS) so the verdict checks
  against a stated acceptance.
- Path 1 stays as the catch-up for router decisions that close non-code items (questions, operator steps, superseded lanes).

## Backlog catch-up
After Path 1 is live: a one-off router audit lists accepted lanes without a Closes token; the router adds the tokens (or one close batch
under OS-92), md-sync closes them. No hand batch per item.
