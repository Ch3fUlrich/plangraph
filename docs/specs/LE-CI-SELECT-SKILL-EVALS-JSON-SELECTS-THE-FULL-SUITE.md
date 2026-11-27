# LE-CI-SELECT-SKILL-EVALS-JSON-SELECTS-THE-FULL-SUITE

Outcome. A change to `skills/<name>/evals/<file>.json` selects the skill tests (pytest only), like a skill page, instead of the full suite.

- plan_source. Keeper item plangraph:LE-CI-SELECT-SKILL-EVALS-JSON-SELECTS-THE-FULL-SUITE, 2026-11-27, priority 10, D-7047, D-7052.
- base commit. 443bf493af86a2ccd1d2859fb4e1d52eceea522d
- card SHA-256. 664de84c8e3c74d6e4e78eada450bfc45aa7b3af67c631b012622745227e0d32
- tree SHA-256. d75cd080d8174c6586b9918c6aea4876f82e78c6c05904dc2598116ff1725806
- evidence_after_deploy. No deployment: the selector runs from the checkout. Record the fast-checks log of the next skill-only head (skill page plus evals.json) with `pytest=true`, `full=false` and a total wall under 300 s.

## Checked against base

- `select()` sends every path that is not known to the full suite (`tools/ci_select.py:239-243`). `_is_known()` accepts the docs class (any `*.md`, `docs/**`) and the prefixes `src/ tests/ tools/ vendor/ rust/` (`tools/ci_select.py:28-34,199-212`). A `skills/*/evals/evals.json` path is neither, so it selects `full=true` with every job.
- The per-path loop maps `skills/**.md` to pytest only (`tools/ci_select.py:256-259`). `tests/test_ci_select.py::test_skill_page_change_runs_pytest` and `::test_skill_non_page_change_is_unknown_full` pin both sides today.
- Readers of `evals.json`: `tests/test_skill_evals.py` (all skills) and `tests/test_planning_skill_token.py:57-58`. Both are pytest. No Rust source or test reads an `evals/` file (grep over `rust/` finds none), so pytest is the complete selection.
- `tools/ci_select.py` is 790 lines against the 800-line limit (`tools/check_file_size.py:47`) and is not in `.filesize-baseline`.
- `tools/**` maps to `CONTRIBUTING.md` in `.docs-map`. `CONTRIBUTING.md` is 800 lines. `tools/check_changelog_docs.py:394-400` accepts a new `docs/contributing/<ITEM>.md` fragment instead.

## Decisions

- D1. One predicate `_is_skill_test_input(p)` covers both skill inputs: `skills/**.md` (unchanged) and exactly `skills/<name>/evals/<file>.json` (four path segments, third is `evals`, name ends `.json`). Deeper or other JSON under `skills/` stays unknown, so it still runs the full suite. Use segment checks, not `fnmatch`, because `*` in `fnmatchcase` crosses `/`.
- D2. `_is_known()` returns true for the predicate. The loop's existing skill branch uses it: `pytest=true`, then `continue` (no `pytest-postgres`, no Rust, no browser).
- D3. Net growth of `tools/ci_select.py` is at most 8 lines (the file must stay at or under 800). No other selector behaviour changes.

## Entry points

- E1. `tools/ci_select.py::select` and `main select --files ...` output.

## Acceptance

| Row | Entry | Observable result | Evidence |
| --- | --- | --- | --- |
| B1 | E1 | `select(["skills/x/evals/evals.json"])` gives `pytest=true`, every other suite false, `full=false`. The CLI prints `pytest=true`, `rust=false`, `browser=false` and `full=false`. | X1 X2 |
| B2 | E1 | A skill page plus its evals.json selects the same as the page alone. | X1 |
| B3 | E1 | `skills/x/evals/sub/a.json`, `skills/x/evals.json` and `skills/x/scripts/x.sh` still select `full=true`. | X3 |

## Verification

Write X1-X3 first. On the base X1 and X2 fail with `full=true`.

- X1. `tests/test_ci_select.py::test_skill_evals_json_runs_pytest_only`. Assert B1 and B2 on `select()`.
- X2. `tests/test_ci_select.py::test_skill_evals_json_cli_prints_no_rust_browser_or_full`. Call `main(["select", "--files", "skills/x/evals/evals.json"])` with `GITHUB_OUTPUT` removed. Parse the `name=value` lines and assert B1.
- X3. `tests/test_ci_select.py::test_skill_json_outside_evals_stays_full`. Assert B3. The existing `test_skill_non_page_change_is_unknown_full` stays unchanged.
- X4. Held out: `python3 -m pytest -q tests/test_ci_select_rust_inputs.py tests/test_skill_evals.py`, unchanged.

## Scope and reuse

- R1. Extend the existing skill-page rule. No new suite and no new table.
- N1. Rust-input tables (`RUST_INPUTS`, `RUST_WORKER_INPUTS`) are unchanged. `skills/batch-writer/SKILL.md` and the other listed skill pages still select Rust through them.

### File budgets

- `tools/ci_select.py`: at most 8 lines added, final at most 798.
- `tests/test_ci_select.py`: at most 40 lines added.
- `docs/contributing/LE-CI-SELECT-SKILL-EVALS-JSON-SELECTS-THE-FULL-SUITE.md`: new, at most 6 lines. It states that a skill's `evals/*.json` selects the skill tests like its pages.
- `changelog.d/LE-CI-SELECT-SKILL-EVALS-JSON-SELECTS-THE-FULL-SUITE.md`: new, at most 4 lines.

### Implementation

- Add `_is_skill_test_input` after `_is_docs_class`, with a one-line docstring naming `tests/test_skill_*.py` as the guard.
- In `_is_known`, return true when the predicate holds. Replace `if p.startswith("skills/") and p.endswith(".md"):` in `select()` with `if _is_skill_test_input(p):` and keep its comment.

## Open points for design approval

None.
