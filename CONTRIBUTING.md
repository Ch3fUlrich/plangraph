# Contributing

Run `python3 tools/preflight.py --base origin/main --head HEAD` before pushing:
it blocks on scrub, CI policy (including commit trailers), changed-crate Rust checks,
and affected tests, in that order. Each family ends with a timed PASS/FAIL/SKIP/INFRA
summary; failed checks with HTTP 5xx output in any family (including policy) are
inferred as INFRA and retried once; persistent INFRA exits 3, other failures exit 1.
Small checks have a 60-second timeout; cargo clippy/test have 600 seconds each and
affected pytest has 900 seconds. The hook has no additional overall preflight bound.
Use `--keep-going` to collect all failures or `--fast`
to skip affected tests. The existing pre-push hook runs preflight too; set
`PREFLIGHT_FAST=1` for its fast mode or `PREFLIGHT_SKIP=1` to bypass only that hook
invocation (not the existing scrub/ref/file-size gates or direct CLI runs). Private
scrub words come from `PLANGRAPH_SCRUB_WORDS` or `~/.config/plangraph/scrub-words.txt`;
when neither is present, public rules still run but scrub reports SKIP with a reason.
`PREFLIGHT_TOOLS_DIR` overrides tool paths for tests.
The revision controls `FILESIZE_REQUIRE_BASE`, `NEWCODE_BASE` and `CHANGELOG_BASE`
are scoped to policy checks. Other families receive none of these controls,
including inherited values, so nested Git test fixtures own their revision bases.
Affected tests also clear Git's repository-local environment reported by
`git rev-parse --local-env-vars`, including indexed configuration pairs. Policy
checks retain the caller's Git context; test repositories keep their own hooks.

plangraph is public; the plans it tracks usually are not. Keep that difference clean.

## Setup and tests

Python 3.11+ (CI runs 3.12). `tests/conftest.py` puts `src/` on `sys.path`, so there is no
install step; the keeper tests need Linux, macOS or WSL, the rest run anywhere.

```bash
python -m pip install -r requirements-ci.txt  # dev deps, including pytest-xdist and jsonschema>=4
python tools/affected_tests.py --base origin/main --run -n auto  # writers / PR / L2
python -m pytest -q -n auto tests            # main merge + nightly full suite
```

The affected selector falls back to the full suite for unmapped Python source,
shared fixtures and suite configuration; documentation/status/build artifacts do
not trigger a full run. Do not bypass a conservative fallback on PRs.
Pass `--head <revision>` for a committed-only comparison, excluding staged,
worktree and untracked changes; omit it to include checkout changes.
Reproduce speed measurements with `bash tools/measure-test-speed.sh both HEAD`;
the fixed runner.py sample does not modify files, and the full run uses a 6 GiB
systemd user scope. Use `affected` or `full` to measure each separately.

The shared Rust workspace is defined by `rust/Cargo.toml` and `rust/Cargo.lock`;
these cover all member crates, not one feature. Map their documentation updates
here in `.docs-map`, while crate-specific paths keep their own feature guides.
Run `cargo test -p <crate>` from `rust/` for each affected crate.

## Lanes

Completed Rust ports remove only their own Python waiver from
`tools/new_code_allowlist.txt`; preserve unrelated exceptions when merging main.
SPAWNER-RUST-PORT slice 2 also removes the stale doctor-source waiver retired by
slice 1; its changelog fragment accounts for all four named Python waivers.
Doctor source-filter goldens live in `tests/fixtures/doctor_sources.json` and run
through both Rust unit tests and `tests/test_ps_host_sources.py`; update both
consumers rather than matching Python source text or spawning Python from Rust.

One branch per lane (`lane/<short-name>`, cut from `main`), one small PR into `main`.
The RUN-WORKTREE Python regression exception (D-5062) retires with SPAWNER-RUST-PORT.
Work that covers two plan items is two lanes.
A finished review seat's verdict is forwarded to the keeper by the spawner (`tools/seat_verdict.py`) only for the item's latest verified head; interrupted sends are not blindly replayed.

New production code must be Rust unless explicitly listed in `tools/new_code_allowlist.txt`; the host-readiness source-probe helper is a single-file exception for the pre-gate lane, retired by `SPAWNER-RUST-PORT`.

For an offline judge brief, run `python3 tools/judge_prompt.py --findings findings.txt
--base origin/main --head HEAD --ci-summary ci.json --output judge.md`, with
`ci.json` mapping job names to conclusion strings. The prompt includes complete
findings and diff; `--byte-budget` limits whole file bodies and lists explicit cuts.
See [Lean review harness](docs/lean-review-harness.md) for seat tool modes.
Only `--tool-mode tools` advertises follow-up reads; `inline-only` reads the
spawner-inlined diff without tools, and `prompt-only` uses supplied evidence.
Gateway prompt-only config is file-backed via a portable launcher, never argv.
New Python files require a reasoned rule 9.14 entry in `tools/new_code_allowlist.txt`.

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
The file-size gate refuses malformed Git batch headers with a single reason line.

## Changelog and documentation

Every change touching non-documentation paths requires an entry (rule D-653), either as a new or edited fragment that adds an entry line against the merge-base in `changelog.d/<ITEM-KEY>.md` (several parts of one item: `<ITEM-KEY>-<n>.md`) holding the entry line, or as a new non-heading line under `## Unreleased` in `CHANGELOG.md`. Fragment PRs keep open lanes mergeable because each PR adds its own file; `python3 tools/changelog_fold.py` folds all fragments under `## Unreleased` newest first, then deletes only fragments whose entry lines were folded. Fragments must be direct children (no nested paths); README.md is excluded. Uncommitted fragments sort newest by mtime with filename ties, before committed fragments ordered by first-commit date. Empty or heading-only fragments remain in place and are reported; `--check` writes nothing and exits 1 while fragments remain. An empty fragment or deleting the changelog or editing whitespace does not count. A non-doc change may waive this only when every commit that touches non-doc paths carries its own `Changelog: skip (<reason>)` line in its trailer block (last paragraph; merge commits do not count), with a non-empty reason (for typos or format-only edits). Furthermore, repository documentation mappings in `.docs-map` pair path globs with their corresponding doc files; changes touching a mapped path must update at least one of its listed docs. Globs: `*` stays inside one path segment, `**` spans segments; the `# enforce-after:` date is the first day the mapping is an error (earlier days warn). `python3 tools/check_changelog_docs.py` (range `merge-base(base, --head)..head`) verifies this in CI and runs with `--warn-only` on each pushed sha in the pre-push hook.

`tests/fixtures/writer_docs_map.json` is the shared glob/parser case table for Rust writer finalization and `test_writer_docs_map_shared_parity` in `tests/test_check_changelog_docs.py`; run the latter with `python3 -m pytest -q -n 2 tests/test_check_changelog_docs.py`. Rust reads the head map; CI additionally retains merge-base rules (see [writer finalization](docs/SPAWNER.md#writer-result-finalization)).

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
Or set `PLANGRAPH_PUBLIC_REMOTES` to comma-separated remote substrings (empty items ignored) and run `python3 tools/check_push_gate.py --install PATH`; audit with `python3 tools/check_push_gate.py --root DIR` (unset or empty: all remotes are public; installation refuses without writing).

The private word list is never stored in the repo: point the hook at it via environment,
`PLANGRAPH_SCRUB_WORDS=<path to words.txt>` (optional extra allowances: `PLANGRAPH_SCRUB_RULES=<path to rules.toml>`).
Unset, the hook uses `~/.config/plangraph/scrub-words.txt` (requires `chmod 600`).
A missing default warns once that the private-word rule is inactive; public patterns still run.
An explicit missing or unreadable list refuses pushes to any remote. The env list wins.
Remotes are public unless `git config remote.<name>.plangraph-private true` is set:
that skips private-word matching, not public patterns. Classification uses local config only.
Diagnostics never show matched values or the words-file path.
