# HANDOFF — plangraph-L1-main (Sonnet 5.5 → Opus successor), written 2026-10-01 late

You are the new plangraph-L1-main (Opus, high effort, D-232). FIRST: read `CONSTRAINTS.md` (pinned rules, decisions, accepted
defaults) then `status/plangraph-L1-main.card.md`, then this file. Brief: `/home/s/code/routing/docs/plan/briefs/plangraph-L1-main.md`
(has "Scope extension" D-231). You talk only to routing-00 (SendMessage `routing`; a successor router with the same name may answer).
Layers: L0 router, L1 project orchestrator (you), L2 phase orchestrator, L3 task group = writer + reviewer(s) (D-230).

## 1. Where things are
- Repo `/home/s/code/plangraph` (local git, no remote — ask routing-00 before adding one). Work lives on branch `worktree-proposal` in
  worktree `/home/s/code/plangraph/.claude/worktrees/proposal` (you start there; `main` only has the first commit).
- Writers' working repo (code only, no site data): `$CLAUDE_JOB_DIR/tmp/keeper-code` = `/home/s/.claude/jobs/33c5cb14/tmp/keeper-code`
  (branch `master`). Writers are spawned with cwd there; their sandbox branches are fetched from
  `/home/s/code/AutoOS/logs/sandboxes/AutoOS-<run-id>` (`git fetch <sandbox> agent/<run-id>`; fast-forward master; then copy into the
  worktree with `git checkout FETCH_HEAD -- src tests docs` from `git fetch <keeper-code> master`). R-coord-01: NEVER move keeper-code
  master while a child run is live. (If `$CLAUDE_JOB_DIR` differs after relaunch, the keeper-code dir still exists at the path above.)
- Docs: `docs/PROPOSAL.md` (M1, approved), `docs/SPEC.md` (item/snapshot/views contract), `docs/KEEPER.md` (keeper contract), `docs/RUNBOOK.md`.
- Code: `src/plangraph/{views,views_graph,render}.py`, `src/plangraph/web/*`, `src/plangraph/keeper/*` (28 modules: schema, db, events, auth,
  store*, evidence*, api, cli*, daemon, config, importer, mcp, fmt). Data: `data/plan-v3/*.yaml` (93 items; checker `tools/check_import.py`),
  `data/source/plan-v3/` (copy of the prose plan @routing 29a032d), `vendor/` (cytoscape 3.34.0, dagre 0.8.5, cytoscape-dagre 2.5.0, MIT).
- State at hand-off: worktree HEAD ≈ constraints commit after `1ef63ee`; **497 pytest green** (~85 s; `python3 -m pytest -q`), src 6,167 lines,
  tests 6,201, every file ≤ 400. Throwaway round trip script: `$CLAUDE_JOB_DIR/tmp/roundtrip.py` (loads data/plan-v3 into a temp DB, checks
  validate/ready/export==import/verify_chain/verify_integrity). NOTHING is loaded into a real store yet.

## 2. In-flight / just finished work
- **X6b** (writer run `20261001-200303-x6b-opus-findings-ds-be475a`, pinned deepseek) was RUNNING at hand-off: Opus-review fixes
  (changes_requested counted over the item's lifetime → count per epoch+round; family names restricted to `[a-z0-9._-]+`, refused at
  add_principal/import owner_family; waiver binding: D-row must contain `Waives: <project:ID>` as whole token; cancel clears waiver +
  waiver truthiness; KEEPER.md residual-risk paragraph; report which tests prove `_reviewer_family_ok`). To collect: `mcp__autoos-agent__result`
  with that run id; if done, fetch/ff-merge into keeper-code, copy into the worktree, run the full suite, commit. If it FAILED (the previous try
  X6 `…194417-x6-opus-findings-ds-4268af` died on a provider 504) re-spawn the same task (text = the X6b task in this session; items (1)-(6) above).
- Reviews so far (D-219 two seats): seat 1 = in-session Sonnet subagent (session-local, GONE after recycle — spawn a fresh `Agent` with
  model sonnet, adversarial brief, see §6), seat 2 = muse-spark pinned (`model: omniroute/spark-1.3-contributor`, card role=review,
  privacy=public, cwd keeper-code). Rounds 1–5 ended PASS/PASS on the pre-tier code. routing-00 did an Opus spot review (accepted M2a).
- Paid spend ledger: pending (gateway usage unreadable until OS-41 key). Paid run ids: every writer/reviewer named a1…x6b in this session
  (pin-probe 170002; a1 170020; a2 170020; b0 170929; b1/b2 171430; b3 172600; h1 172011; h2 172529; h1b 173904; h1c 174541; h1d 175348;
  h1e 181457; x1/x2 181855; x3 183211; x4/x4a 183952/184256; x4b 184948; x5 191604; spark seats 171450,180057,182955,185911,194001).
  Back-fill from the gateway when routing-00 can read it. Never read opencode session DBs.

## 3. NEXT STEPS (in this order)
1. Collect/merge X6b (above).
2. **X7 = D-228 tiers + the three accepted defaults** — spec in `docs/handoff/X7-tasks.md` (three sequential small writer tasks X7a/X7b/X7c;
   deepseek writers die at ~128k context: keep each task's read list explicit and small, tell them to learn failures from pytest output only).
3. Two-seat re-review of the X7 diffs (fresh Sonnet subagent + muse-spark). Fix findings (small tasks), re-review until both PASS.
4. Add `review_tier: high` to the D-228 items: `python3 $CLAUDE_JOB_DIR/tmp/add_tier.py` (run in the worktree; idempotent text edit of
   data/plan-v3) after the importer supports the field; run `tools/check_import.py` and the round-trip script.
5. **Pilot keeper on coding.vm** (labelled "isolation not effective (pilot)"): follow docs/RUNBOOK.md. Config (JSON, mode 600): db
   `/home/s/.local/share/plangraph/keeper.db`, snapshot `/home/s/.local/share/plangraph/plan.snapshot.json`, bind 127.0.0.1:8787,
   `repos: {server: /home/s/code/Server, autoos: /home/s/code/AutoOS}`, `runs_dir: /home/s/code/AutoOS/logs/agents`,
   `decisions_file: /home/s/code/routing/docs/decisions-log.md` (NOT DECISIONS.md). `pg admin init`, `add-principal` for `routing-00`
   (role router, projects all five, family anthropic) writing its token DIRECTLY to `/home/s/.config/plangraph/router.token` (0600) —
   capture with a shell redirect/sed, NEVER display it; likewise a token file for yourself (role l1, project plangraph). The CLI reads
   token files via PG_TOKEN_FILE. Start `python -m plangraph.keeper.daemon --config …` in the background (nohup), health-check `/v1/health`.
6. **Load data/plan-v3** (router GO given) with `pg admin import data/plan-v3 --as routing-00`; verify counts (93 items, 0 problems, 19 ready),
   `pg verify`. Then the T7-BOARD sequence (order matters): router `cancel T7-BOARD` reason "operator 2026-10-01: plan graph replaces the board; no
   kanban"; then router `edit` GOAL depends_on dropping T7-BOARD; then T7-CONSOLE title/done_criteria "plan-graph page is its board view (no board integration)".
7. `pg render --cdn --out /home/s/code/plangraph/out/plan-cdn.html` (the page generator also takes a snapshot file); tell routing-00 (they
   publish it as a private artifact for the operator). Desktop/phone screenshots wait for M3 (workstation).
8. **Router update batch**: `/home/s/code/routing/docs/plan/graph-updates-2026-10-01.md` → `data/updates/2026-10-01-router.yaml` (A = 15 state
   changes: use set_legacy_state / close_on_decision(batch_ref = file path + sha256) / add_evidence_router(kind commit|decision) / splits =
   cancel original with reason 'split' + new done/open halves + re-point dependents + GOAL dep; B = 19 new items with owners, deps,
   review_tier, evidence kind). Validate (no cycles, all non-terminal reach GOAL), send routing-00 the diff + `pg ready --all --brief`,
   load ONLY after routing-00's OK. From then on the router changes definitions only through the keeper.
9. Reply "handoff ready" is already done; report milestones to routing-00 (short, with paths/evidence).
10. **THEN D-231 scope extension (M2c)**: best-practice research via cited researcher subagents (Sonnet/Haiku with web tools; operator dashboards for
    agent fleets, human-in-the-loop approval UX, graph views at 100–500 nodes on desktop+phone, cost/telemetry presentation, OIDC/CSRF-safe
    write paths, stats sources: gateway usage ledger needs OS-41, Claude usage), then write the M2c proposal (data model: project clusters →
    phases → tasks + questions/decisions/operator tasks as first-class blocking items; GUI write path + Authelia OIDC; insight/stats; migration of
    QUESTIONS.md + decision log; test plan; cost/time). routing-00 judges, operator approves at a milestone. M2b (off-host keeper by prox via Semaphore,
    run-bound tokens, signed snapshot + outbox, hash-chain anchor, Postgres decision at M4) is a prerequisite for the GUI write path in production.

## 4. Accepted decisions to implement (from routing-00; details in CONSTRAINTS.md)
- D-228 tiers (standard 1 reviewer family ≠ writer; high 2 distinct families; waiver high only; router-only lowering; defaults by keyword + import list).
- Default 1: tier `none` only when evidence_kinds == [decision]; router verb `close_on_decision(key, batch_ref)` records batch file path + sha256 (keeper verifies the
  file hash); decisions-log row `Closes: <project:ID>` required when present. Default 2: legacy evidence kind `commit` (exists + ancestor of main, no trailer/date/CI),
  router-only via add_evidence_router. Default 3: router verb `set_legacy_state(key, state, note)` open→claimed|in_review only, flagged legacy.
- Waiver: D-row contains `Waives: <project:ID>`. Family names `[a-z0-9._-]+`.

## 5. Known gaps / residuals (all stated to routing-00)
Raw-SQL writer (keeper OS user/root) can forge `review_passed` for an unlocked key — only the M2b anchor detects it; verifiers not re-run inside the write txn
(id-set + epoch + state recheck instead); merge-commit rejection skipped on purpose; semaphore-task adapter API shape UNVERIFIED (fakes only), real gh/semaphore tests = M3 (prox
has a test template); pilot on coding.vm: agents run as user `s` in docker/lxd groups so OS isolation is NOT effective; deepseek/spark runs record family "unresolved"
(spawner gap; D-206 evidenced only by the in-session Sonnet seat); `pg render` needs the snapshot; no browser on coding.vm.

## 6. How to run the review seats (copy)
- Seat 1: `Agent` (subagent_type general-purpose, model sonnet): read-only on the repo; scratch only under `$CLAUDE_JOB_DIR/tmp/seatN/`; adversarial: break the DONE locks, role/tier rules, races
  (two Store connections, hooks between `_reverify_all` and commit), raw SQL on the keeper connection, injection; report ≤ 15 numbered findings with severity + repro, end `VERDICT: PASS|FAIL`.
- Seat 2: `mcp__autoos-agent__spawn` with `model: omniroute/spark-1.3-contributor`, `card {role: review, complexity: standard, privacy: public, spend: credit}`, `claude_reason: n/a`,
  cwd = keeper-code, READ-ONLY task text with the same hunting list; the run record tags it "no-verdict" when the text lacks the exact marker — read the text, it ends with VERDICT.
  If muse is down > 1 h: seat-2 waiver (D-217) = Sonnet seat + routing-00 spot check, noted. D-219/D-228: exactly one implementer + the tier's reviewers, no extra "final".
- Waiting pattern: background Bash `until [ -f <logs>/agents/<run-id>/exit.json ]; do sleep 15; done` (run_in_background), then `mcp__autoos-agent__result`.
  If a classifier denial appears: stop, report the exact text to routing-00, never route around it.

## 7. Process learnings
deepseek (128k context) dies on big read lists → small tasks, explicit file lists, `grep -n`/`sed -n` ranges, failures from `pytest --tb=line` only; provider 504s happen → re-spawn; pinned free OpenRouter models
(`or-*`) are unservable ("Unable to determine provider"); privacy=sensitive cannot pin trial legs (use a code-only repo + privacy public for generic code); heavy runs (browsers, installs, builds) go to the
workstation via routing-00; disk was 99 % (routing says 58 GB free now). Worktree guard refuses complex git one-liners — use plain separate commands.
