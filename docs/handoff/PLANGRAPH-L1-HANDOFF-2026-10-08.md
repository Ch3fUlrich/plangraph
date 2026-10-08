# plangraph-L1 handoff — 2026-10-08 evening (D-717 recycle)

Read first: `status/plangraph-L1-main.card.md`, `CHANGELOG.md` (2026-10-08), this file. Memory: terse output, decisions
via routing, Haiku rule, L2 rule (≥ 3 worker runs → L2 via routing).

## State
- Live tree = this worktree (`worktree-proposal`), pushed; services run from it: `plangraph-keeper.service` (systemd-run
  user unit, Restart=on-failure), `plangraph-md-sync.{timer,path}` (fetches all keeper repos first), `plangraph-render.timer`.
- Keeper verify ok. Repos in keeper.json: server (origin/improve), autoos, routing, plangraph, workst-gen (gen-analysis),
  great-wiki, invest (all origin/main). Backups `~/.local/share/plangraph/keeper.db.pre-*`, `keeper.json.pre-*`.
- Tailnet: `tailscale serve --https=8787` → keeper (interim, D-713). prox-L1 via ssh relay (forced command in
  `~/.ssh/authorized_keys`). Principals + token files: see `docs/operations/plangraph-mcp.md`.
- Page: new UI live on plan.ohje (graph overview, table, Q&A, Needs You, Activity, operator-wait colours).

## In flight
- Nothing. PG-BRIEF merged (lane/pg-brief c675581), keeper restarted on it; full suite 1402 passed, 2 pre-existing
  failures (test_split_examples; test_split_make_public: README.md:79 high-entropy string from a01c8ed) → lane
  PG-SPLIT-EXAMPLES-TEST.

## Next (order)
1. **PG-MODELS** (D-708 A, spec routing-worktrees/server-L1-routing/docs/plan/plangraph-single-source-2026-10-08.md):
   verb `record_run` (new role `spawner`; create principal `spawner` after it lands), columns writer_model, seat_models
   (JSON), run ids, created_at, done_at; derived reviewed/merged in show + snapshot; migration + integrity fold agree
   (copy how `wait` was added: 537d063). One writer → no L2.
2. PG-FETCH-ON-DEMAND, PG-CI (GitHub Actions pytest), PG-SPLIT-EXAMPLES-TEST (pre-existing failure), PG-PROXY-ACCESS
   (reverse proxy like omnigraph, then `tailscale serve --https=8787 off`).
3. Open UI lanes: UI-CREATED-AT, UI-NODE-BUDGET, RENDER-NEWUI-DETECT; MODEL-KINDS (+ D-656 keeper gaps).

## How work is done here
- Batches: `data/updates/*.yaml` → trial on a DB copy (`tmp/trial/keeper.json`, port 8797) for big ones → backup →
  `tools/apply_batch.py … --token-file ~/.config/plangraph/router.token` → `admin verify` → QUEUE.md line → commit.
  Pin `source_sha256` right before apply (the decisions log changes often). Edits only on open items (tier edits ok on
  claimed). Cancel needs the gate dep removed first.
- Closes only via ROUTER D-rows (by = router/routing-00, `Closes:` in the decision cell, commit items need `@sha`).
- Writers: autoos-agent spawner, free legs, code-only repo (`git archive src tests tools`), never plan data to free legs;
  seat = `review-call.py --model ovh/gpt-oss-120b` on the diff (it reads hunks only — verify every claim; most are false).
- Keeper-side changes: lane branch → server-L1-routing judges → merge → restart keeper if store/API changed.
- Real-browser checks: render → Taildrop to `bz-wg-pdw028` → workst-L1-autoos checks at 1440x900 / 1280x800 / 390x844.
