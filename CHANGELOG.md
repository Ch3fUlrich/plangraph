# Changelog

Merged and pushed changes only (D-630, D-653). Newest first.

## 2026-10-23

- **RT-OVERSIGHT-CHECKS-FOLLOWUP:** `oversight-check` now publishes the digest as a reader-atomic triple: `digest.json` and `digest.sha256` stay for compatibility, and the new `digest.pair.json` (`{"sha256":…,"text":…}`, same directory) is renamed into place *last*, so a crash between any two writes leaves the previous complete pair readable. New `cli_oversight.read_digest(DIR)` prefers that commit record; a directory written by an older run without it is accepted only when the two legacy files agree, otherwise it raises `DigestPairError` — it never returns a pair whose sha does not describe its text (tests simulate a crash after the json and after both legacy files). `oversight.raise_alarms` refreshes the `detail` of an already-open `(kind, key)` alarm row in place on a changed finding, keeping its `id` and `raised_at` and opening no second row. Tests also pin R4 continuing to report every other lane when one ref's `rev-list` fails, and `_table_exists` treating a raising catalogue query as "no table".
- **KEEPER-OPTASK-CLOSE:** merged to main as `9200bd7e` (PR #129); this line is the post-claim evidence commit (D-2330).
- **KEEPER-OPTASK-CLOSE:** a claimed/in_review `operator_task` now closes through `close_on_decision` at any review tier (`standard` or `high`) when the router calls it, the decision evidence verifies, and a decision row names the key in a whole `Closes: <project:ID>` token — a non-`none` tier is never authorised by the hashed batch file alone. Tier `none` behaviour is unchanged (a `Closes:` token is still optional there), lane/question/goal at a non-`none` tier still refuse `bad_state`, a decision naming another key is still `evidence_unverified`, non-router callers stay `forbidden`, and the kind/tier pair is re-checked under the write lock so a change between pre-lock and commit is `conflict` (`src/plangraph/keeper/store_close.py`).
- **KEEPER-OPTASK-CLOSE:** tests only (red): `tests/test_keeper_optask_close.py` pins that a claimed/in_review `operator_task` closes through `close_on_decision` at `standard` or `high` only when a decision row names the key in a whole `Closes: <project:ID>` token, that lane/question/goal at a non-`none` tier still refuse `bad_state`, that non-router callers stay `forbidden`, that a decision naming another key is `evidence_unverified`, that a kind/tier flip under the write lock is `conflict`, and that the same close works inside `apply_ops` while a non-operator refusal rolls the whole batch back.
- **SEAT-COVERAGE-KEEPER:** keeper `verdict` now refuses to close an item until every writer family of its current-epoch verified `commit+ci` evidence is covered by the round's reviewer seats (`Refused('coverage', ...)`, pure `plangraph.keeper.coverage.uncovered` mirroring `tools/seat_verdict.coverage`; `need=1` standard, `2` high with a non-anthropic seat, two Claude seats counting once); fail-closed `writer_unknown` for a missing/unknown `Writer`, a trailers error, or a commit with neither `Writer` nor `Hand-Edit` (a `Hand-Edit` commit is `anthropic`); new `GitAdapter.writer_trailers` reads the non-merge commits in `merge-base(p1,p2)..p2`, so PR/PHASE merge evidence takes writers from the leaf branch commits and ignores the merge commit; `ok=False` and non-closing high verdicts are unchanged.

## 2026-10-22

- **SEAT-COVERAGE-KEEPER (tests only):** red tests in `tests/test_keeper_seat_coverage.py` for the keeper refusing to close an item until every `Writer:` family of its current-epoch commit+ci evidence is covered by the posted reviewer families (mixed writers, two Claude seats counting as one family, sensitive high items, standard tier, missing/unknown trailers and `trailers()` errors failing closed as `writer_unknown`, decision-only items unchanged, `ok=False` exempt, and atomic refusals), plus a `trailers_by_sha` extension to `FakeGit` in `tests/evidence_fakes.py`; the keeper-side check is not implemented yet.
- **RT-P1-FLOW-UNBLOCK (follow-up):** tests only: non-router `add_dep` via `apply_ops` on a claimed item is refused (`forbidden`); the router's add_dep on a claimed item asserts its `item_defined` event; length assert in the claim-without-base test.
- **PG-JAIL:** Require fully enforced Landlock ABI V2 so cross-root rename/link is supported without a best-effort bypass. Replace broad procfs read grants with opt-in process-local and selected system-file access to protect sibling environments. Strengthen escape and resource-limit assertions, avoid default systemd probes, and remove unused exec/dependency code.
- **WORKER-SANDBOX:** Give each jailed attempt a private, drop-cleaned tmp root and `TMPDIR` instead of shared temp write access. Require and canonicalize absolute existing `PG_JAIL_BIN` files; configure the local e2e test profile explicitly without weakening production startup.
- **PG-JAIL / WORKER-SANDBOX (review round 2):** Require Landlock ABI V3 so `truncate(2)` outside the writable roots is denied (fail closed with exit 78 below V3). Run card `check_cmd` through the attempt jail and resource scope, add the harness binary directory read-only, require `PG_TEST_ALLOW_UNJAILED=1` with `PG_JAIL=off`, and fail resource-limit/landlock test skips when `CI=true`.

## 2026-10-19

- **SPAWNER-CATALOG-DB:** SQLite Model & Benchmark Catalog (`src/plangraph/spawner/catalog_schema.sql`, `catalog_seed.py`, `catalog_db.py`): WAL-mode SQLite catalog storing 22 Artificial Analysis v4.3.2 model benchmarks, quota pools, providers, clients, and role routes (`l1-orchestrator`, `l2-worker`, `l2-reviewer`, `l3-driver`), plus deterministic JSON/OmniRoute combo exporters (`export-json`, `export-combos`).
- **SPAWNER-TIER-LADDER:** 5-Rung Tier Priority Order (`TIER_RUNG`): Enforces `trial (0) < free (1) < credit (2) < subscription (3) < paid (4)` so subscription pools (`agy`, `cxa`, `mimo`, `opencode-go`, `claude`) are strictly prioritized before pay-as-you-go (`deepseek`, `gemini` API).
- **SPAWNER-RANKING-GUARDRAILS:** Verified AA v4.3.2 Model Ranking & Guardrails: Promotes `mimo/mimo-v2.6-pro` (`46.0` AA Intelligence, `51 tok/s` overall / `29 tok/s` direct, `$0.13/task`, `92.4%` cache hit rate, deeper `mimo` subscription token pool) ahead of `opencode-go/qwen3.8-max` (`45.4` AA Intelligence, `85 tok/s`, `$0.45/task`); sets `cxa/gpt-6-luna-high` (`38.4` Intel, `180 tok/s`, `$0.07/task`) as the sole active Codex ACP (`cxa`) model while gating `astra` and `sol-*`; clamps `claude-sonnet-5-5*` `max` effort to `xhigh`; and guards against the Haiku 100K token price cliff.
- **SPAWNER-OMNIROUTE-RESOLVER-MCP:** OmniRoute ACP-First Spawner, Resolver & MCP Server (`measure.py`, `risk.py`, `clients.py`, `resolver.py`, `runner.py`, `mcp.py`): Routes all providers through OmniRoute ACP/Gateway first with offline CLI fallback, preserves Windows `SystemRoot` (`D-2090 BUG-B`), injects `OPENCODE_CONFIG_CONTENT` (`baseURL` + `X-OmniRoute-*` envelope headers), and enforces strict cross-family code reviews (`writer.family != reviewer1.family != reviewer2.family`).
- **SPAWNER-HOST-BOOTSTRAP:** Host Bootstrap Delegation (`deploy/requirements-host.json`, `deploy/bootstrap.ps1`, `deploy/bootstrap.sh`): Delegates host package provisioning (`opencode-cli`, `tailscale`, `omniroute`, agent CLIs) to `AutoOS` without code duplication.

## 2026-10-18

- **RT-P1-VERBS-LIVE-GUARDS:** `/v1/park` now checks the claim holder/delegate, an unexpired lease and an optional body `epoch` like heartbeat/submit (a stale worker cannot park a re-claimed card; router/system exempt; a holder's park of a `claimed` card releases the claim). Parking a claimed card now also revokes the card's run tokens, as release does. Merge locks are lease locks, not store-side fencing, whose owner is bound to the calling principal and stored as `<principal>:<owner>` (`%` and `:` in the principal name are percent-escaped, so the first `:` always ends the principal segment; echoed by acquire and required verbatim by release; cross-principal acquire and release are 409). Tests in `tests/test_keeper_worker_verbs.py`.

## 2026-10-17

- **MODEL-NAMES:** `tools/names_backfill_dryrun.py SNAPSHOT.json [--json]` lists (never applies) the name backfill
  candidates of a plan snapshot: one sorted line per active item's `title_is_id` / `no_description` warning with a
  proposed title derived from the key and the head of the note's folded `description: ` line — read-only (no DB, no
  write verb, no network), exit 0 unless the input cannot be read (2); direct `views.warnings` tests cover both codes.
- **PG-CI-FORGEJO:** `commit+ci` can verify a project whose CI runs on Forgejo Actions: `src/plangraph/keeper/evidence_forgejo.py` (`ForgejoStatusAdapter`) reads `GET {base}/api/v1/repos/{o}/{r}/commits/{sha}/statuses?page=&limit=` (operationId `repoListStatusesByRef`) with `Authorization: token <T>` (token re-read per call from `forgejo_token_file`, refused when missing/empty/a symlink, never in argv, URL, logs, a raised message or `repr`; stdlib `urllib.request`, https except `127.0.0.1`, hard timeout, `max_bytes` body cap, `max_pages` cap that raises on truncation), and normalises `CommitStatus` → check-runs keeping the newest status per `context` so a rerun's stale failure cannot block a green sha (unknown status fails closed). New `ci` value syntax `forgejo:<owner>/<repo>` delegates from `CIAdapter` (a `forgejo:` value without an adapter raises, a plain slug still uses `gh`); new config keys `forgejo_url` + `forgejo_token_file` (both required, `Config.__repr__` stays key-names-only). Hermetic tests on a 127.0.0.1 fake (`tests/test_evidence_forgejo.py`); proposed config diff in `docs/proposals/keeper-ci-config.md` (not applied — token mint, live `keeper.json` edit and keeper restart are operator steps).

## 2026-10-16

- **MODEL-LOOP:** item kind `loop` (free-text `kind`, no schema change) with an exit decision: `views.validate` reports `loop_no_exit` when a non-cancelled loop has no exit and `loop_many_exits` when it has more than one — an exit is a non-cancelled `decision` item whose `depends_on` lists the loop; `ready`/`blocked`/`waves`/`critical_path` treat a loop like a lane and the keeper accepts `kind: loop` at create (tests `tests/test_views_loop.py`, `tests/test_keeper_loop_kind.py`; `docs/SPEC.md` note).
- **RT-P1-GOVERNOR:** budget governor (`src/plangraph/governor/`, `docs/governor.md`): keeper `metrics` table (schema v11, PG migration 4) with `set_metrics`/`get_metrics`/`log_governor_override`, daily job `python -m plangraph.governor metrics daily`, pure `allow()` policy (never blocks answers, operator-started sessions or operator-requested judges; non-Claude fallback when Claude is over 1/7 burn or rate limited; provider quota throttle for worker legs; overrides and fallbacks logged), and `planner.start.guard_start`.

## 2026-10-15

- **PLANNER-KIT:** `src/plangraph/planner/` (cards, readiness gate, routing legs, context pack) plus `service.py` gluing it to the keeper store (`gate_card` moves items to `ready` / `needs_input` with one planner question, `route_for_card`, `pack_for_card`, `import_batch`); see `docs/PLANNER-KIT.md`.
- **PG-BACKUP-RETENTION:** `pg admin prune-backups --dir D [--keep N] [--daily-days N] [--apply]` trims ad-hoc backup files in a data dir (dry run by default, explicit allow-list, keeper files never touched); opt-in keeper `backup_prune` config hook, default off.
- **RT-P1-CONTRACT:** keeper contract 1.0 frozen in `docs/contract/` (prose, route reference, JSON schemas) with a storage-agnostic black-box suite in `tests/contract/` (subprocess keeper, or `PG_CONTRACT_URL` plus tokens). Keeper fixes: malformed `n`/`lease_s`/priority are 400 not 500; `verdict` `ok` must be a JSON boolean.

## 2026-10-11

- **QA-BACKFILL:** `tools/qa_backfill.py --questions F --out DIR` proposes choices for QUESTIONS rows without a valid Options block (`DIR/proposed-rows.md` + `DIR/review.md`, deterministic, writes nothing else).

## 2026-10-10

- **PG-AUDIT-META (D-1012/D-1013):** keeper read verb `audit_meta` — items in `open`/`claimed` missing `description`,
  `estimate_h` (NULL **or** `estimate_src: default`), `done_criteria`, `owner`, with `gate` and the operator kinds exempt
  from estimate and criteria; claims whose newest event is older than N days (default 3, injected clock) grouped by
  claimant (owner fallback) with the action to take — `close-by-D-row` on verified commit evidence (the snapshot's
  `merged` rule, no git call), `release` for an unreachable claimant or > 2N days quiet, else `renew`. Doors
  `pg audit-meta [--project|--stale-days|--skeleton DIR]`, `GET /v1/audit-meta`, MCP `audit_meta` (32 tools); router and
  L1 only, an L1 sees its own projects (another project via `--project` → 403). `--skeleton DIR` writes one
  `P.BATCH.yaml` per project of empty `op: edit` stubs beside its sha-pinned note, which
  `tools/apply_batch.py --dry-run` accepts (49e4e8d).

## 2026-10-09

- **SYNC-MD-IMPORT + md-sync Kind column:** QUESTIONS rows become question/operator_task, answered rows in
  Answers with D-ids (d931e98, cc138a6). **FU-X7-LOWS:** keyword prefix fix + evidence-shape integrity check
  (2119ae6). **PROGRESS-PANEL** (ec7b30d), **UI-TABLE-PHONE** (946cceb), **UI-MORE-NODE-EXPAND** (46abd5a).

## 2026-10-08

- **MODEL-KINDS (D-231/D-292/D-656):** item kinds question/decision/operator_task (operator/wait defaults), fields
  layer/role/agent_id/phase_kind/fixes, evidence kind `error`, router verb `answer` (event `answered`, locks like a
  decision close; old DBs get their lock triggers rebuilt on open), reads `needs_you` / `answers`; MCP 28 tools
  (aeb0208, L2-mk + plangraph-L1, judge ACCEPT).
- **UI lanes (L2-ui) + keeper proxy files:** table created column from snapshot created_at, node budget 200 per
  expanded phase with a '+N more' node, render new-UI detection on content (97d0ded). Keeper proxy (not live):
  nginx sidecar with mutual TLS + edge allowlist, mint-certs.sh for the shared internal hop CA (b00ea79, D-759 A).
- **PG-FETCH-ON-DEMAND / PG-SPLIT-EXAMPLES-TEST / PG-CI:** keeper fetches a stale origin/* clone once per repo per
  60 s when commit evidence misses (b8ea51b); examples regenerated + README pointer reworded, full suite 0 failures
  (5c029fa); GitHub Actions pytest on push/PR (fffd19f, run 37850230244 green).
- **PG-MODELS (D-708 A):** keeper verb `record_run` (new role `spawner`, principal created) records run ids, writer
  model and seat models per item (e2b07d9, judge ACCEPT server-L1-routing); show/snapshot add created_at, done_at,
  reviewed, merged. md-sync status close_skipped carries the reason (c9edf6c).
- **Activity page (D-696):** keeper snapshot carries 31 days of state events (b8c731b); header page Activity with
  24 h / 7 d / 30 d / custom ranges, counts + lists per project (workst browser PASS). Render drops whole-line JS comments.
- **Operator waits (D-677):** keeper item field `wait` = answer|task (537d063); md-sync sets it from QUESTIONS.md,
  imports priority-table rows and suffixed OS ids (f7e59d1); page outlines items/phases/projects amber (answer) or
  violet (task), legend, Needs You badges, dependent tooltip (1f1d044, 2cd6d37). Decision text on the Decisions
  page (8c18e56). md-sync fetches all keeper repos first; workst-gen repo registered.
- **Page (later 2026-10-08):** Q&A answers update counts and row badges live (797c428, 2d53adb); URL hash keeps
  collapse/focus/filter state (59ff024); tests for table view, Q&A page, sidebar resize (f06ceb6); page_side.js /
  page_ui.js split under 400 lines + size guard (ef4c67f); Decisions table reads keeper ref objects (2d53adb).
- **Page (new UI live):** opens on the collapsed project overview fitted to all hulls; data-guarded
  width/height styles (no cytoscape mapping warnings); Settings reachable on phone; dead duplicate of 16 functions
  removed from page_boot.js (04b1d78). Phase filter uses phase||track, `state:` facet is case-insensitive, sidebar
  tables capped at 300 rows (f856375). Real-browser check PASS at 1440x900, 1280x800, 390x844.
- **Keeper + md-sync:** supersede rule: the latest router D-row wins a key's `Closes:` sha; same-row conflicts stay
  invalid and are reported (9a8fd66). Keeper config: plangraph ref = origin/main. Daemon runs as the user unit
  plangraph-keeper.service.
- **Sync:** main (a01c8ed: header pages, Table, Q&A, Projects tree, skill plangraph-planning) merged with the live
  tree; restored `views.warnings` (keeper alerts), dropped stale real-name fixtures, keeper e2e tests on the
  fictional projects (1286436, 8902d06).
- **Docs:** review record docs/reviews/2026-10-08-since-1006-seats.md; layered-planning gap table and task-size
  options docs/proposals/2026-10-08-layered-planning-gaps.md (operator chose size B, D-656).
- **Plan data:** 2026-10-08 router batches under data/updates/ (see data/updates/QUEUE.md).
