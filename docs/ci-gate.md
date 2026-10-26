# CI gate

`tests.yml` runs on every pull request to `main`, every push to `main`, and manual dispatch.

The `select` job (`tools/ci_select.py`) maps changed paths to suites: `scrub`, `pytest`, `pytest-postgres`, `rust`, `rust-worker`, `rust-spike`. Unknown paths, or a change under `.github/`, `requirements-ci.txt`, or `tools/ci_select.py`, run every suite; a docs-only change runs `scrub` only.

The `ci-gate` job fails if any selected suite failed, was skipped, or was cancelled.

Branch protection on `main` requires only the `ci-gate` check — no review requirement, admins not enforced. Rollback: delete the protection.

A `Commit trailers` step (`tools/ci_commit_gate.py`) checks each PR commit for a non-Claude `Writer:` trailer and, when tests are removed or renamed, a `Test-Removal:` trailer naming every removed test (`Test-Removal: D-<n> <test id>[, <test id>...]`). Exempt shas are listed in `tools/ci_commit_exempt.txt`.

A Claude Writer passes only in the form `claude-<model> <id>` (optional trailing note in parentheses, free text but no second `Writer:` marker and no newline), where `<id>` is a spawner run id (`run-<32 hex>`), a Claude Code session id (`session_<id>`), or a uuid; the id is checked for shape only, never looked up. Any other Writer value carrying a Claude marker is refused as `claude writer without run or session id`.

## Known limits of the commit gate

- Gate-Edit ids are checked for shape (D-<digits>) only, not against the decision log. A `Test-Removal:` trailer is read only from the final trailer block and must name every test the commit removes or renames: `Test-Removal: D-<n> <test id>[, <test id>...]`; a bare `D-<n>` is refused.
- Claude Writer run/session ids are likewise checked for shape only, never looked up: an invented `run-<32 hex>` (e.g. 32 zeros) passes.
- The first push of a new ref (all-zero before sha) is not gated, a pull request into main still is.
- Merge commits are skipped, their parent commits are checked.
- The Claude marker list matches the Writer value by substring and relies on the writer naming itself.
- Copies of gate files under other names are not path-matched (only suffix variants such as .bak are).
- Test removal is detected by name in files whose path looks like a test (tests/ directories, test_* files, *_test.* and *.test.*/*.spec.* files), unconventional locations and deleted conftest.py fixtures are not detected.
- The ci-gate step wiring is checked by tools/gate_wiring_check.py, which the merger runs before merging (it compares the ci-gate job and triggers with the base and demands a Gate-Edit trailer, in the trailer block, on every commit that touches the workflow file; it cannot verify the id).
- Hand-Edit commits are capped at 50 changed lines (interim ceiling, operator 2026-10-25), may not touch the auth/door surface and are listed in the gate log.
- For a Writer-trailer commit the gate checks the trailer text, not that a writer really ran.