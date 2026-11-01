# Contributing

plangraph is public; the plans it tracks usually are not. Keep that difference clean.

## Setup and tests

Python 3.11+ (CI runs 3.12). `tests/conftest.py` puts `src/` on `sys.path`, so there is no
install step; the keeper tests need Linux, macOS or WSL, the rest run anywhere.

```bash
python -m pip install -r requirements-ci.txt  # dev deps, including pytest-xdist
python tools/affected_tests.py --base origin/main --run -n auto  # writers / PR / L2
python -m pytest -q -n auto tests            # main merge + nightly full suite
```

The affected selector falls back to the full suite for unmapped Python source,
shared fixtures and suite configuration; documentation/status/build artifacts do
not trigger a full run. Do not bypass a conservative fallback on PRs.
Reproduce speed measurements with `bash tools/measure-test-speed.sh both HEAD`;
the fixed runner.py sample does not modify files, and the full run uses a 6 GiB
systemd user scope. Use `affected` or `full` to measure each separately.

Tests set git identity only via environment variables (`identity_env`) or `-c` options (`identity_args`), never via `git config`. The pre-push hook refuses pushes (`fixture-identity`) if the repository's local git config carries a fixture identity. That check reads only `--local` config: a fixture identity that arrives through `GIT_AUTHOR_*`/`GIT_COMMITTER_*` (what `identity_env` sets) or through global config is not seen, and the hook does not scan the pushed commits' authors, so such commits still push.

## Lanes

One branch per lane (`lane/<short-name>`, cut from `main`), one small PR into `main`.
Work that covers two plan items is two lanes.

## File size

A tracked source, test, script or doc file stays at or under 400 lines; over 400, split it
by responsibility before adding more. `python3 tools/check_file_size.py` enforces this in CI
and in the pre-push hook. Files that were larger when the gate landed are listed in
`.filesize-baseline`: such a file may shrink, never grow, and the baseline itself only
shrinks: `--update-baseline` writes it once when none exists, afterwards only lowers
counts, and CI refuses an entry added or raised against the change's base (`origin/main`
on a PR, the pushed-over commit on a push to main). Data and vendored
files are not counted; a deliberate exemption goes in `.filesize-allow` with a reason
(today only `CHANGELOG.md`, an append-only ledger).

## Commits

Every commit message ends with one trailer paragraph — no blank lines inside it:

```
Plan-Item: plangraph:<KEY>
Co-Authored-By: <author> <noreply@example.com>
```

## No secrets in the history

This repo is public: never commit a password, token, hostname, IP address, e-mail address
or home path — not in a diff, not in a commit message. Keep
`python -m pytest -q tests/test_split_make_public.py` green: it is the guard that keeps the
public split public, and it fails on exactly those leaks. CI runs on GitHub-hosted runners
only.

Install the pre-push scrub gate (scans the files changed in the pushed commits with
`tools/split/scrub_check.py`, refuses on any hit, prints only `file:line: rule`):

```bash
git config core.hooksPath tools/hooks        # per clone; undo: git config --unset core.hooksPath
```

The private word list is never stored in the repo: point the hook at it via environment,
`PLANGRAPH_SCRUB_WORDS=<path to words.txt>` (optional extra allowances: `PLANGRAPH_SCRUB_RULES=<path to rules.toml>`).
Unset, the hook still runs the public patterns and prints a one-line notice.
