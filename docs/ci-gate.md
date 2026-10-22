# CI gate

`tests.yml` runs on every pull request to `main`, every push to `main`, and manual dispatch.

The `select` job (`tools/ci_select.py`) maps changed paths to suites: `scrub`, `pytest`, `pytest-postgres`, `rust`, `rust-worker`, `rust-spike`. Unknown paths, or a change under `.github/`, `requirements-ci.txt`, or `tools/ci_select.py`, run every suite; a docs-only change runs `scrub` only.

The `ci-gate` job fails if any selected suite failed, was skipped, or was cancelled.

Branch protection on `main` requires only the `ci-gate` check — no review requirement, admins not enforced. Rollback: delete the protection.

A `Commit trailers` step (`tools/ci_commit_gate.py`) checks each PR commit for a non-Claude `Writer:` trailer and a `Test-Removal:` trailer when tests are removed. Exempt shas are listed in `tools/ci_commit_exempt.txt`.
