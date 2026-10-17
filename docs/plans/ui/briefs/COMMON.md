# UI-STABLE lanes: common brief (read first)

These are plan items under gate `plangraph:PH-UI-STABLE`, owned by build-L1. The router applied them as D-1730; the operator chose to
fix the static page now.
- Design: `docs/plans/ui/2026-10-17-ui-plan.md` (repros R1-R10 in section 3).
- Spec: `docs/ui/VIEWS.md` (acceptance tags `[V-…]`).

## Start gate and order

- Run at most 3 lanes at once. The order:

  | Lane | Items | Starts |
  |---|---|---|
  | U1 | UI-VERSION-STAMP, then UI-DEPLOY-AUTO | at once |
  | U2 | UI-BROWSER-TESTS | at once |
  | U3 | UI-GRAPH-CLICK, then UI-GRAPH-EXPAND, then UI-URL-HISTORY | after U2 merges |
  | U4 | UI-NARROW-LAYOUT and UI-FLAKE-FIXES | after U2 merges |
  | U5 | UI-DEAD-CODE | last |

- Claim each item in the keeper before work, heartbeat while working, and attach evidence before submit.
- Lane branches only: a new worktree per lane, branch `lane/<item-id-lowercase>`. Never merge. The router merges after the review
  passes.

## Writers, seats, evidence

- Code is written by writer runs ONLY via the autoos-agent MCP `spawn` (D-430/D-431). Do not write code in-session.
- Models: model order D-1714. Seat family = the model actually served, different from the writer and from each other (D-1705).
  agy gets code only, never plan data (D-562).
- EVERY writer task text carries this line verbatim: "Never change behaviour to satisfy a test, never special-case test
  inputs; fix the code so the real behaviour is right."
- EVERY seat prompt carries this point: "no production code detects or adapts to test inputs".
- Failing test first: each fix starts from its R-test or V-test in the browser suite, red. The fix turns it green. Do not edit a test to
  make it pass; if a test or a spec line is wrong, report it to build-L1 with the reason.
- Standing step D-323: open the rendered page with Playwright at 1440x900 and 390x844, run the lane's V-checks, and store the
  screenshots in the lane report (not in git).
- Evidence `commit+ci` (UI-DEPLOY-AUTO also needs `run-record`).

## Rules of the code base

- One page, no build step: `src/plangraph/render.py` inlines `src/plangraph/web/*.js|css` into `page_template.html`. Every asset stays
  under the 400-line budget (`tests/test_ui_file_size.py`). Add new files to `JS_FILES`/`CSS_FILES` in order.
- Snapshot text goes in as text, never as HTML (V-ALL-3).
- The repo becomes public (PG-PUBLIC-READY): no hostnames, IPs, user names or home paths in code, units, tests or docs. Units are
  templates, filled from env.
- CI runs on GitHub-hosted `ubuntu-24.04` runners and only on PRs into main (`.github/workflows/tests.yml`). A new job must fit that
  setup: path filters, and a timeout under 20 min.

## Report

To build-L1, terse: item, branch, head sha, tests (counts), the V-tags passed, screenshots path, and anything that contradicts the spec.
