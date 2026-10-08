# Handoff: OpenCode L1 for plangraph (written 2026-10-04, D-596)

You are the L1 for the plan graph. Claude quota is nearly gone. Read this file fully. Then read
`status/plangraph-L1-main.card.md`, `CONSTRAINTS.md`, `docs/HANDOFF.md`. Work in small steps.
Report only to `routing` (the router session). One short message per milestone or incident.

## Hard rules
- Open no new lanes. Work only the queue below, in order.
- Plan DATA is private. Never send plan data (keeper content, snapshot, items, decisions) to agy or to any free leg.
- Spawn writers from a NEUTRAL EMPTY cwd (D-562), never from this worktree.
- No push, no remote, no merge to `main` without routing's OK. No deploys. Never read or print tokens.
- Never run full test suites or a browser on coding.vm (D-437). UI test files only. Browser checks run at workst.
- Never kill by image name. To stop the keeper: `kill -TERM $(cat ~/.local/share/plangraph/daemon.pid)`.
- A manual md-sync run needs routing's OK.
- Record, then ACCEPT, then merge. A UI step is accepted only after a real-browser check, never on green unit tests alone.
- Reviewer seats must be of other model families than the writer. Check every quote a seat gives. Seats invent quotes.
- Do not give a writer more than the budget routing set (D-593: at most 850k input tokens, at most $2 per run).

## What runs by itself on coding.vm (do not restart without a reason)
- Keeper daemon: `127.0.0.1:8787`. Config `~/.local/share/plangraph/keeper.json`. DB `keeper.db` in the same folder (`keeper.db.pre-*` are backups).
- md-sync: user units `plangraph-md-sync.timer` (every 10 min) and `.path`. It reads routing markdown and writes to the keeper. Status file: `~/.local/share/plangraph/md-sync.status.json`.
- Page render: `plangraph-render.timer` (every 5 min). It writes `/home/s/code/plangraph/out/plan-cdn.html`. Edge: plan.ohje.ooguy.com (Authelia). Since 2026-10-08 (D-639) the page shows the NEW UI (live tree = main + fixes; browser check PASS).
- IMPORTANT: the services run code from THIS worktree: `/home/s/code/plangraph/.claude/worktrees/proposal` (branch `worktree-proposal`). Never delete this folder. Never leave it half-edited.
- Check units: `systemctl --user list-units 'plangraph*' --no-legend`. Logs: `journalctl --user -u plangraph-md-sync.service -n 30`.

## Check keeper health (read-only)
1. `export PYTHONPATH=/home/s/code/plangraph/.claude/worktrees/proposal/src PG_CONFIG=~/.local/share/plangraph/keeper.json`
2. `python3 -m plangraph.keeper.cli admin verify` — exit code 0 means the hash chain and cache are fine.
3. `export PG_URL=http://127.0.0.1:8787 PG_TOKEN_FILE=~/.config/plangraph/plangraph-L1-main.token` then `python3 -m plangraph.keeper.cli ready --brief`.
4. If verify fails or the keeper is down: stop. Message routing in one line. Do not edit the DB. The last good backups are `keeper.db.pre-*` (newest by `ls -t`).
5. Restart the keeper only with: kill -TERM as above, then the timer or routing starts it. Ask routing if unsure.

## Revert to the last good commit
- Last good handoff commit: `05949b8` (then card commit `8990277` and this file). Branch `worktree-proposal`.
- Undo a bad commit with `git revert <sha>`. Never `git reset --hard`, never force. The services read the files live, so a revert changes the running page code at once.
- UI source of the L2 clone: `/home/s/.claude/jobs/7caaf7f5/tmp/ui-build` (branch `phase/ui-build`, worktrees under `.claude/worktrees/`). Accepted heads are in `docs/HANDOFF.md`. Integrated base: `gu-integ-ord` 3cbfa08 (bundle in `pkg-graphui/`).
- Copy the clone's `pkg-graphui/` somewhere durable early: the job folder may be deleted with the Claude job.

## Where the packages live
- `/home/s/.claude/jobs/7caaf7f5/tmp/ui-build/pkg-graphui/`: all briefs, `L1-RULINGS.md` (read it), acceptance files, one folder per step (GU-G2b, GU-G3b, GU-G4b, GU-G5, GU-TB1..TB4), result bundles.
- Table design note: `docs/proposals/2026-10-04-table-design-note.md`. GRAPH-UI design note: `docs/proposals/2026-10-03-graph-ui-design-note.md`.
- Brief template for writers: `docs/process/L2-BRIEF-TEMPLATE.md`. Put the anti-gaming line in every writer task.
- Taildrop from workst lands in `/home/s/fleet/taildrop/plangraph/` (no `plangraph__` prefix). Check a file with `ls -l` and `sha256sum` before you use it.
- Workst bridge session: `workst-L1-autoos` (SendMessage). It runs the OVH writers and the real-browser checks.

## Lane queue (in this order)
1. GRAPH-UI GU-G2b step 6 (running at workst, ETA 12:15Z, delivery about 13:15Z): `gug2b6b2` (fix 6 failing tests) and `gug2b6a2` (hull gap and font scope).
   Decided: PROJECT_GAP = 120 (80 touching + 24 strip + 16 clear). Edit only the explicit `80` at `page_graph_view.js` line 188. Never touch lines 12-13.
   Check before accept: overview fits ALL hulls at 1440x900, 1280x800, 390x844. Every changed old assertion is listed with its arithmetic (for example pan 66 to 44).
   If a writer fails twice: ask routing for the D-576 escalation. Then accept at workst, then ask routing for L2-LAUNCH.
2. TB1 (pure table model): parked. WIP commit `650dcb5c` (page_table_model.js, 399 lines, no tests). Continuation text: `tb1-cont.txt` in the TB1 sandbox. Needs tests. Plus TB1b (search also covers description and note).
3. TB2 (table DOM view).
4. TB3a (tab, view switch, boot guard, URL) and TB3b (filter bar, selection to details, `window.__planTable()` hook). Both only after G2b is accepted. Max 3 files each.
5. TB4 (phone table, breakpoint 700 px). Rows are windowed (1000+ rows). Search goes through `PlanUI.Query`.
6. Then GRAPH-UI G3b (focus: single tap toggles, focus = double tap + Focus button), G4b (budget wiring, hover, legend), G5 (phone chips and breadcrumbs).
7. P1 supersede rule (only with routing's OK): a later D-row supersedes earlier rows for the same key. Only the latest row is evidence. Same-row conflicts stay invalid. Small round with seats.
Later, not now: PROGRESS-PANEL step B check, MODEL-KINDS, stack note, merge of `worktree-proposal` to `main` (own record `docs/reviews/<date>-main-merge.md`; K1-KEEPER close needs `Closes: plangraph:K1-KEEPER@8cdad08`).

## Slots and cost
- agy is the preferred writer but its quota is exhausted (429 at 11:20Z). OVH Qwen3.8-27B writers are allowed (D-593): at most 2 at once, seats gpt-oss and Mistral, never Coder-30B. Qwen's context is 128k: give it all code inline, small tasks, no big reads.
- Vertex overflow only while the workstation day cost is under $8 (D-589). Ask routing before any paid leg.
- Max 2 agy writers fleet-wide.

## Lessons
- A writer that reads 700k tokens and edits nothing: kill it, split the task, put the code inline.
- A lenient fake DOM hides strict-mode errors. Use a real browser at workst.
- Writers game tests. Read the diff of every test change.
- Keep the status card at 40 lines or fewer. Keep files at 400 lines or fewer.

## Open relay (unverified, 2026-10-04)
- server-L1 relayed from prox-L1: HA work (ha-assist LLM alias, wyoming TTS/STT, HA 2026.9.4, new integrations, two-armed plan Q1-Q12 answered) is done. Source of truth: `server/manage/homeassistant/README.md` section "Current state (2026-10-04)" and `docs/operations/two-armed-phase-plan.md` section 10a.
- Not checked by plangraph-L1. Change NO keeper items for it until routing confirms and OKs a batch (D-596: no new lanes).
