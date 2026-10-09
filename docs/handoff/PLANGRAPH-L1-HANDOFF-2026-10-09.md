# plangraph-L1 handoff — 2026-10-09 early (session 01F17Egz…, after D-783)

Read first: `status/plangraph-L1-main.card.md`, `CHANGELOG.md` (2026-10-08/09), this file. Memory: terse output,
decisions via routing, Haiku rule, L2 rule.

## State
- Live tree = this worktree (`worktree-proposal`, head after 9f5521e), pushed; services run from it (keeper,
  md-sync timer+path, render timer). Keeper verify ok with the stricter evidence-shape check (FU-X7-LOWS).
- Shipped this session (all judged by server-L1-routing, live): PG-MODELS (record_run, principal `spawner`),
  PG-FETCH-ON-DEMAND, PG-SPLIT-EXAMPLES-TEST, PG-CI (Actions pytest), PG-PROXY-ACCESS files (mutual-TLS sidecar,
  NOT live), UI-CREATED-AT / UI-NODE-BUDGET / RENDER-NEWUI-DETECT (L2-ui), MODEL-KINDS (5 lanes, L2-mk),
  UI-MORE-NODE-EXPAND, PROGRESS-PANEL, UI-TABLE-PHONE, SYNC-MD-IMPORT + md-sync Kind-column fix, FU-X7-LOWS.
- Spawns on coding.vm frozen by routing (memory floor) → I wrote lanes myself; seats via gateway review-call
  (deepseek-v4.1-flash, groq-qwen3.8-27b, ovh/gpt-oss-120b work; kimi legs 402/502).

## Open / waiting
- Closes owed by routing (D-rows): UI-TABLE-PHONE@946cceb, SYNC-MD-IMPORT@d931e98, FU-X7-LOWS@2119ae6, UI-SHELL@a01c8ed
  (evidence sent); main ff to the live tip.
- PG-PROXY-ACCESS go-live parked (operator mints the shared "ohje internal hop CA"; then listener/vhost/cutover).
- MODEL-HOST: done per D-775 except workstation:T0-WSCLONE (in_review, waits for Q-WSCLONE-REPOINT).
- Next by graph: SPLIT-DATA-REPO (prio 2), then PG-M2B / API-PUSH / MODEL-LOOP (prio 0).

## How work is done here (unchanged)
Batches: data/updates/*.yaml, pin source_sha256 right before apply, backup (sqlite backup API), apply_batch,
admin verify, QUEUE.md line, commit+push. Before an edit of a gate's depends_on drop cancelled deps (validation).
Router can release a claim (POST /v1/release with router token) before a rehome.
Lanes: own worktree under .claude/worktrees/<lane>, full suite (~5-15 min), make_public check, seats, judge →
merge → restart keeper if store/API changed → render. Browser checks: Taildrop to bz-wg-pdw028, workst-L1-autoos.
