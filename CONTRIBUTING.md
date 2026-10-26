# Contributing

plangraph is public; the plans it tracks usually are not. Keep that difference clean.

## Setup and tests

Python 3.11+ (CI runs 3.12). `tests/conftest.py` puts `src/` on `sys.path`, so there is no
install step; the keeper tests need Linux, macOS or WSL, the rest run anywhere.

```bash
python3 -m pip install -r requirements-ci.txt  # dev deps, including pytest-xdist
python3 tools/affected_tests.py --base origin/main --run -n auto  # writers / PR / L2
python3 -m pytest -q -n auto tests            # main merge + nightly full suite
```

The affected selector falls back to the full suite for unmapped Python source,
shared fixtures and suite configuration; documentation/status/build artifacts do
not trigger a full run. Do not bypass a conservative fallback on PRs.
Reproduce speed measurements with `bash tools/measure-test-speed.sh both HEAD`;
the fixed runner.py sample does not modify files, and the full run uses a 6 GiB
systemd user scope. Use `affected` or `full` to measure each separately.

## Lanes

One branch per lane (`lane/<short-name>`, cut from `main`), one small PR into `main`.
Work that covers two plan items is two lanes.

## Commits

Every commit message ends with one trailer paragraph — no blank lines inside it:

```
Plan-Item: plangraph:<KEY>
Co-Authored-By: <author> <noreply@example.com>
```

## No secrets in the history

This repo is public: never commit a password, token, hostname, IP address, e-mail address
or home path — not in a diff, not in a commit message. Keep
`python3 -m pytest -q tests/test_split_make_public.py` green: it is the guard that keeps the
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
