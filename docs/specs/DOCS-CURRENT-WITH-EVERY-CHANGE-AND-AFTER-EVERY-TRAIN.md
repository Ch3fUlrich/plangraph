# DOCS-CURRENT-WITH-EVERY-CHANGE-AND-AFTER-EVERY-TRAIN

Outcome. The docs gate refuses a head that adds a source file without a .docs-map mapping (against a shrink-only ratchet of today's unmapped paths), and after every landing `plangraph docs-audit` re-runs the docs gate over the landed range, flags docs citing a path or symbol the range removed, and files one keeper item per gap naming its commits.

- plan_source. Keeper item plangraph:DOCS-CURRENT-WITH-EVERY-CHANGE-AND-AFTER-EVERY-TRAIN, operator 2026-11-27 13:04Z (D-7053, rule 3.113), priority 10, rule D-653.
- base commit. 443bf493af86a2ccd1d2859fb4e1d52eceea522d
- card SHA-256. 037f84f2515692092084558189dad25aedbd478dbc7fa565b5fc3aed6b0b54a7
- tree SHA-256. 3e55e05649529152bace0d1d56ab2c902839d0cd16b2c9ea175d99f9e8b70ddc
- evidence_after_deploy. After the first train that lands with this change and the release install that carries the new `plangraph` binary to the landing host: the landing output (stderr of `tools/merge_batch.py land` or `landing-record`) shows the `docs-audit:` summary line with range, gap count, filed count and ratchet count, and `check-changelog-docs:` on that head prints the `docs-coverage` count. Record both lines in the item evidence. Rule text for rule 3.113 enforcement is routing's step (item How (5)), not code.

## Checked against base

- `tools/check_changelog_docs.py` enforces `.docs-map` only for mapped paths and knows no unmapped source files; `.docs-map` has 27 mappings. [tools/check_changelog_docs.py:285-358; .docs-map]
- Part (2) of the item is already true on main: fast checks (`tools/preflight.py`, policy family) and CI (`.github/workflows/tests.yml` job `changelog-docs`) run the checker without `--warn-only`; only the pre-push hook demotes to warnings. Nothing to change there. [tools/preflight.py:639-645; .github/workflows/tests.yml:122-133; tools/hooks/pre-push:388-395]
- Because fast checks and CI both run `check_changelog_docs.py`, a coverage rule inside it reaches both without touching `preflight.py` (787 lines) or `tests.yml` (803 lines).
- New code must be Rust (D-4691): pg-policy refuses an added `.py` outside `tests/**/test_*.py` unless `tools/new_code_allowlist.txt` lists it with an OPERATOR D-id. Editing existing Python files is allowed. So the coverage rule extends the existing checker and the train audit is a Rust subcommand. [rust/pg-policy/src/main.rs:9-13,170-225; tools/new_code_allowlist.txt]
- Landing paths: `tools/merge_batch.py land` (tested-ff and train-pr-merge) and `tools/merge_batch.py landing-record` (a landing made by another tool, e.g. the router's merge-part). `merge_batch_land.py` and `merge_batch_util.py` are at 800 lines; `merge_batch.py` is at 269. The lander runs keeper tools as subprocesses that never fail the land (`close_merged_items` runs `plangraph merge-close`). [tools/merge_batch.py:52-56,137-154,225-262; tools/merge_batch_util.py:788-800]
- Rust keeper transport for write verbs exists and is public: `plangraph_cli::feeder::{Env, SlowKeeper, Transport}`. [rust/plangraph-cli/src/feeder/adapters.rs:43-55; rust/plangraph-cli/src/feeder/mod.rs:14]
- `CONTRIBUTING.md` (the doc of `tools/**`) is at 800 lines; a new `docs/contributing/<ITEM>.md` fragment stands in for it in the gate. [tools/check_changelog_docs.py:313-318]

## Decisions

- D1. Source file: a tracked file at head under `rust/`, `src/`, `tools/` or `deploy/` that is not a doc path (`is_doc_path`). Mapped: matches a `.docs-map` glob at head. Unmapped set `U`.
- D2. Ratchet `.docs-map-unmapped.json` at the repository root: `{"unmapped": [sorted paths]}` (JSON, so the 800-line gate skips it). Generated once by `python3 tools/check_changelog_docs.py --write-ratchet` at the implementation head; `--write-ratchet` writes `U` when the file is absent at `origin/main`, and otherwise writes `R_base ∩ U` (it may only remove entries).
- D3. Coverage problems, appended to the checker's `problems` (strict fails, `--warn-only` warns): (a) each path in `U - R_head`: `<path> is a source file without a .docs-map mapping: add one`; (b) each path in `R_head - R_base` when `R_base` exists: `<path> was added to .docs-map-unmapped.json: the ratchet only shrinks`; (c) each path in `R_head - U`: `<path> is mapped or gone: remove it from .docs-map-unmapped.json`; (d) ratchet present at merge-base and absent at head: `.docs-map-unmapped.json removed`. No ratchet at either revision: coverage is skipped (fixture repos and other projects). The summary line gains `docs-coverage <len(R_head)> unmapped`. These lines start with `docs-coverage:` so callers can tell them apart.
- D4. `plangraph docs-audit [--repo DIR] [--range BASE..HEAD | --cursor PATH] [--project P] [--gate KEY] [--dry-run]`. Cursor default `~/.local/state/plangraph/docs-audit/<basename of repo>.cursor` holding the last audited `origin/main` sha. Range in cursor mode: cursor..`origin/main` when the cursor is an ancestor; otherwise (absent or not an ancestor) `<head>^1..<head>`. The cursor is written (atomic, private) only after the run finished.
- D5. Gaps: (1) the docs-map and `docs-coverage:` problem lines (`FAIL:` prefix) of `python3 tools/check_changelog_docs.py --base BASE --head HEAD --cwd <repo>` run strict through the shared Python launcher; changelog-fragment problems are not docs gaps. (2) Stale citations: in `docs/**/*.md`, `README.md` and `CONTRIBUTING.md` at HEAD, excluding `docs/specs/`, `docs/briefs/`, `docs/reviews/`, `docs/reports/`, `docs/proposals/`, `changelog.d/` and `CHANGELOG.md`, each backticked token that is a repository path (contains `/`, no whitespace, no `..`, no `://`) or `path::Symbol`; stale when the path exists at BASE and not at HEAD, or the symbol occurs as a whole word in that file at BASE and not at HEAD. Citations already broken at BASE are not this train's gap.
- D6. Commits of a gap: `git log --format=%h BASE..HEAD -- <path>` (the cited path for a stale citation).
- D7. Filing: one `create_items` call per gap through `SlowKeeper { env, max_time_secs: 30 }`: id `DOCS-GAP-<first 10 hex of sha256("<kind>:<target>")>` (kind `unmapped`, `mapped-without-doc`, `stale-path`, `stale-symbol`), project `--project` (default `plangraph`), kind `lane`, priority 8 (rule 3.59: LESSONS-ENFORCE items start at p8), `under` `--gate` (default `plangraph:LESSONS-ENFORCE-A-A`), description `Why:` range, gap line, commits; `How:` add the mapping and the doc, or correct the citation; done criteria `python3 tools/check_changelog_docs.py passes on a head that fixes <target>`. A refusal for an existing key is reported `already filed`; a refusal whose message names `needs_spec` is retried once with `"needs_spec": true` (the spec-admission gate, once landed). `--dry-run` prints the items and files nothing.
- D8. Output: one line per gap `DOCS-GAP <key or -> <kind> <target> commits <shas>` and the summary `docs-audit: <BASE12>..<HEAD12> gaps <n> filed <m> ratchet <count or ->`; exit 0 except usage errors (2) and an unreadable repository (1). The audit never fails a landing (D9).
- D9. `tools/merge_batch.py` registers `land` and `landing-record` through two thin wrappers that call the existing command and, when it returned 0, run `plangraph docs-audit --repo <repo toplevel>` as a subprocess with a 300 s timeout, copying its output to stderr; a missing binary, timeout or nonzero exit prints `docs-audit not run: <reason>` and keeps the land's exit code.
- D10. The new Rust module is mapped, so the item's own head passes D3(a) without a ratchet entry: `.docs-map` gains `rust/plangraph-cli/src/docs_audit* -> CONTRIBUTING.md` (the doc of the landing tools; this item's new `docs/contributing/` fragment stands in for it). The ratchet is generated after that mapping, so it holds only paths unmapped before this item.

## Entry points

- E1. `python3 tools/check_changelog_docs.py` (fast checks, CI, pre-push) including `--write-ratchet`.
- E2. `plangraph docs-audit` and the two `tools/merge_batch.py` landing commands.

## Acceptance

| Row | Entry | Observable result | Evidence |
| --- | --- | --- | --- |
| B1 | E1 | A head adding `src/pkg/new.py` with no mapping and no ratchet entry fails with the D3(a) line naming the path; with a mapping and its doc it passes. | X1 |
| B2 | E1 | Ratchet growth, a stale ratchet entry and a removed ratchet each fail with their D3 line; a repo with no ratchet at either revision prints no coverage problem; `--write-ratchet` never adds an entry once the file is on `origin/main`. | X2 |
| B3 | E2 | A fixture range with one mapped change lacking its doc and one unmapped new file yields two create calls (D7 ids, priority 8, `under`, commits named); a second run reports `already filed` for both. | X3 |
| B4 | E2 | A range deleting `src/a.py` while `docs/x.md` cites `` `src/a.py` `` and `` `src/b.py::gone` `` (symbol removed) yields both stale gaps; a citation broken before BASE and one in `docs/specs/` yield none. | X4 |
| B5 | E2 | The `land` wrapper runs the audit after a land returning 0, not after a refusal, and an audit failure keeps exit 0. | X5 |

## Verification

Write X1-X5 first; on the base they fail (no coverage lines, no `docs_audit` module, no wrapper).

- X1. `tests/test_docs_coverage.py::test_an_unmapped_new_source_file_fails_the_gate` (tmp repos with `changelog_docs_util.git` / `commit_files`, ratchet committed at base).
- X2. `tests/test_docs_coverage.py::test_ratchet_only_shrinks_and_stays_exact` (parametrized over D3 b/c/d), `::test_repositories_without_a_ratchet_skip_coverage`, `::test_write_ratchet_only_removes_once_on_main`.
- X3. `rust/plangraph-cli/src/docs_audit_tests.rs::one_item_per_gap_names_its_commits` (fixture git repo with `tools/check_changelog_docs.py` and `tools/ci_commit_gate.py` copied in, Python through `rust/test_support/python.rs`; a recording `Transport` that answers the second run with the existing-key refusal).
- X4. `rust/plangraph-cli/src/docs_audit_tests.rs::stale_citations_of_removed_paths_and_symbols` and `::cursor_advances_only_after_a_finished_run`.
- X5. `tests/test_merge_batch_docs_audit.py::test_land_runs_the_audit_only_after_a_land` (monkeypatch `merge_batch_land.cmd_land` and `subprocess.run`).
- X6. Held out: `tests/test_check_changelog_docs.py`, `tests/test_check_changelog_docs_range.py`, `tests/test_preflight.py` stay green unchanged (a mapped change without its doc still fails as today).

## Scope and reuse

- R1. One docs checker: the coverage rule lives beside the docs-map rule in `check_changelog_docs.py`; the audit runs that checker instead of reimplementing it and files through the existing Rust keeper transport.
- N1. No change to `preflight.py`, `tests.yml`, `merge_batch_land.py`, `merge_batch_util.py`, the pre-push hook or the new-code allowlist; `.docs-map` gains only the mapping of the new module (D10). Mapping the ~1,178 unmapped paths is follow-up work the ratchet tracks. Rule text (item How (5)) is routing's.

### File budgets

Per-file caps are in the card `line_caps` and the TSV. The ratchet is data (JSON); its line count follows the measured unmapped set at the implementation head (about 1,178 paths in the 13:0xZ data).

### Implementation

- tools/check_changelog_docs.py. Functions `source_paths(root, rev)`, `unmapped(root, rev, compiled)`, `read_ratchet(root, rev)` (`git show rev:.docs-map-unmapped.json`, `None` when absent) and `coverage_problems(root, merge_base, head_sha, compiled)`; call it after Rule B and count its lines as docs-map errors; `--write-ratchet` per D2.
- rust/plangraph-cli/src/docs_audit.rs. `pub fn run(env: &Env, args: &[String]) -> i32` and `fn audit(repo, range, filer: &dyn Transport, dry) -> Report` per D4-D8; `#[cfg(test)] mod docs_audit_tests;`.
- lib.rs / main.rs. `pub mod docs_audit;` and the `docs-audit` dispatch beside `merge-close`.
- tools/merge_batch.py. D9 wrappers `cmd_land_audited`, `cmd_landing_record_audited`.
- .docs-map. The D10 line.
- .docs-map-unmapped.json. Generated per D2 after the D10 mapping.
- docs/contributing/DOCS-CURRENT-WITH-EVERY-CHANGE-AND-AFTER-EVERY-TRAIN.md. The coverage rule and ratchet, how to map a path, `plangraph docs-audit`, its gap kinds and item ids.
- changelog one line.

## Open points for design approval

None.
