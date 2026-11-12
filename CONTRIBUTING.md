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
The MCP snapshot guard has a path-specific `tools/new_code_allowlist.txt` entry
with `(D-5062), retired by MCP-RUST`; keep lane exceptions scoped to that lane's files.

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

CI suite selection in `tools/ci_select.py` treats `docs/schemas/**` and its
`RUST_INPUTS` list as Rust inputs even when they are documentation. These inputs
select both `rust` and `rust-windows` via the shared `rust` output without changing
other suites. `tests/test_ci_select_rust_inputs.py` scans `rust/` for include
literals, manifest-relative reads and paths leaving `rust/`; new repository
inputs must be added to `RUST_INPUTS`, not inline to the workflow.

### Rust builds and toolchain bumps

Run Cargo from `rust/` (or a crate directory beneath it) so rustup reads
`rust/rust-toolchain.toml`, the single Rust release pin shared with all CI jobs
through `.github/actions/setup-rust`. Passing `--manifest-path` from the repository
root does not select that toolchain locally. For example:

```bash
cd rust
cargo build -p pg-spawnd --release
cargo fmt --check
cargo clippy --all-targets -- -D warnings
```

**Bump step:** open one PR that changes the channel in `rust/rust-toolchain.toml`
and fixes the new lints, without weakening `-D warnings`. Run every Rust CI job
(`rust`, `rust-windows`, `rust-worker`, `rust-spike`) and require green before
merging. Do not duplicate the version in workflows. The regression check is
`python3 -m pytest -q -n 2 tests/test_ci_rust_toolchain.py`.
It scans every workflow and local composite action, rejecting `RUSTUP_TOOLCHAIN`
overrides, `cargo +<toolchain>`, direct rustup installation/selection and Rust
installer actions outside the shared setup. The shared installer's upstream
action is commit-pinned; update that SHA explicitly in a reviewed PR when needed.

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
Seat records bind manual `tools/seat_verdict.py --key` posting to their recorded `item_key`; a different key or an explicit null is refused before token reads or network I/O. Older records without the field remain usable through the manual CLI. Card/environment identity validation never infers a target from prompt text.
The `evidence.json` path applies the same refusal to every supplied `item_key` or `key`, including explicit nulls and conflicting aliases; evidence without either identity field retains the legacy manual path. Reviewer tokens belong to the seat's model family, not the `--writer-family` being reviewed.
When evidence and a record coexist, the record's `item_key` remains binding even if evidence omits or replaces it; an unreadable or malformed companion record is refused. The record identity check also runs independently of evidence derivation on the record-only path. `--key` is required for all manual invocations, including legacy records; omitting it exits 2 before seat/token reads or network I/O.

New production code must be Rust unless explicitly listed in `tools/new_code_allowlist.txt`; the host-readiness source-probe helper is a single-file exception for the pre-gate lane, retired by `SPAWNER-RUST-PORT`. `src/plangraph/spawner/redact_tokens.py` is a second one: the Python scrubber must mirror the Rust token catalog until that port retires it (D-5279).

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

New Python source outside tests needs a `path | reason (D-id)` line in
`tools/new_code_allowlist.txt`, which `python3 tools/check_new_code_lang.py` reads; for
example the keeper's host-sample store stays Python because the keeper is Python (D-5038).

## Changelog and documentation

Every change touching non-documentation paths requires an entry (rule D-653), either as a new or edited fragment that adds an entry line against the merge-base in `changelog.d/<ITEM-KEY>.md` (several parts of one item: `<ITEM-KEY>-<n>.md`) holding the entry line, or as a new non-heading line under `## Unreleased` in `CHANGELOG.md`. Fragment PRs keep open lanes mergeable because each PR adds its own file; `python3 tools/changelog_fold.py` folds all fragments under `## Unreleased` newest first, then deletes only fragments whose entry lines were folded. Fragments must be direct children (no nested paths); README.md is excluded. Uncommitted fragments sort newest by mtime with filename ties, before committed fragments ordered by first-commit date. Empty or heading-only fragments remain in place and are reported; `--check` writes nothing and exits 1 while fragments remain. An empty fragment or deleting the changelog or editing whitespace does not count. A non-doc change may waive this only when every commit that touches non-doc paths carries its own `Changelog: skip (<reason>)` line in its trailer block (last paragraph; merge commits do not count), with a non-empty reason (for typos or format-only edits). Furthermore, repository documentation mappings in `.docs-map` pair path globs with their corresponding doc files; changes touching a mapped path must update at least one of its listed docs. Globs: `*` stays inside one path segment, `**` spans segments; the `# enforce-after:` date is the first day the mapping is an error (earlier days warn). `python3 tools/check_changelog_docs.py` (range `merge-base(base, --head)..head`) verifies this in CI and runs with `--warn-only` on each pushed sha in the pre-push hook.

`tests/fixtures/writer_docs_map.json` is the shared glob/parser case table for Rust writer finalization and `test_writer_docs_map_shared_parity` in `tests/test_check_changelog_docs.py`; run the latter with `python3 -m pytest -q -n 2 tests/test_check_changelog_docs.py`. Rust reads the head map; CI additionally retains merge-base rules (see [writer finalization](docs/SPAWNER.md#writer-result-finalization)).

Shared `rust/Cargo.toml` and `rust/Cargo.lock` stay unmapped unless an existing workspace-wide doc applies; their mappings follow the head revision so retired crate-specific rules do not block other crates. Other paths retain merge-base and head enforcement.

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

## Shadow merge decision (D-5252, slice 1)

`python3 tools/merge_gate.py facts.json` prints `GO <sha>` (exit 0) or
`REFUSE <code> <detail>` (exit 1); it only reads that file. No collection,
network, merge, GO-row write, verdict post or item closure is implemented.
`merge_gate_core.decide(facts)` is pure and returns one immutable decision.
Facts: `head_sha_at_check`, `head_sha_now` (full lowercase SHA), `required_checks`
(nonempty names), `ci_checks` (complete latest-per-name records with `name`,
`head_sha`, `status`, `conclusion`), `seats` (`family`, raw `verdict` text,
boolean `pinned`, `open_findings` severity strings), `writer_families`,
`changed_paths`, `tier`, `plan_key`, `item_key`, boolean `preflight_present`,
and `main_changed_production_paths` (complete main delta since CI base).
Path lists include deleted paths and both rename sides. Missing facts refuse;
only completed success checks pass (unrequired suites may be skipped).
The trusted future collector owns
completeness, freshness and evidence provenance; this CLI grants no authority.
First refusal wins: head, CI red/pending, seat absent/pinned/family/findings/
acceptance, gate path, tier, PLAN key, preflight, stale green (rule 3.19).
Python imports the existing strict seat parser/coverage and trailer door table;
additional conservative path categories cover docs-map, deployment, allowlists,
redact/auth and gate/check/scrub tools without copying the door list.

The facts object and every CI/seat record require exactly the keys above;
missing, renamed, non-string or extra keys refuse (`ci_pending` for facts/CI,
`seat_missing` for seats). Top-level schema validation precedes the semantic
refusal order; CI/seat record schemas are validated at their respective stages.
Wrong value types, UNKNOWN values and duplicate evidence never yield GO.
Required/check names, writer/seat families, severity lists and normalized path
lists must be unique; every supplied seat must independently parse as accepting.
Malformed CI values refuse `ci_pending`; malformed writer families refuse
`seat_family_is_writer`; malformed seat pinning/findings/verdicts refuse
`seat_pinned`/`finding_open`/`seat_missing`, respectively. Duplicate seat families
refuse `seat_missing`, duplicate changed paths refuse `gate_path`, and duplicate
main-delta paths refuse `stale_green`. LOW-only findings remain nonblocking.
Item/PLAN keys must match `[A-Za-z0-9_.-]+(?::[A-Za-z0-9_.-]+)?`, with no UNKNOWN
component; invalid keys refuse `plan_key_mismatch`. String-list entries must be
nonempty, printable and have no surrounding whitespace. Paths use the same
case/separator/dot normalization for both evidence and imported door patterns.
Both door tables must be nonempty tuple/list collections of valid path patterns;
the authority's existing root globs `roles*` and `scopes*` are intentional
slash-less exceptions. Missing/malformed/duplicate table entries refuse
`gate_path`, including when a table is accidentally replaced by a plain string.
Duplicate JSON object keys at any depth and undecodable or excessively nested
JSON refuse `ci_pending` without a traceback. Decision tests block file,
environment and clock access inside the real parser/coverage dependency calls,
in addition to subprocess, network and verdict-post effects.

### Shadow merge decision slice 2: fail-closed boundaries

`required_checks` must include `ci-gate`, `select`, the live `ci_select.ALWAYS`
table and every suite selected by the existing pure `ci_select.select()` policy
for `changed_paths`. The selector sees both literal and normalized paths so
case normalization cannot silently remove a required suite. Empty, trivial or
incomplete self-declared lists refuse `ci_pending`; each required check must
complete successfully on the exact head. Every supplied check still participates
in red/pending detection, even if it is not required. Invalid selector tables or
results refuse `ci_pending`; the policy is imported, not copied into the gate.

Slice 2 limit: this is a local repository-policy floor, not live GitHub branch
protection/ruleset discovery, workflow parsing or authenticated evidence
collection. Additional workflow jobs (including browser and platform jobs), live
required contexts, workflow/base provenance and completeness/freshness must be
established by the trusted future collector and included in `required_checks`.
No count or self-declared list proves those external facts. This shadow CLI
remains non-authoritative and must not be wired directly to unattended merges.

Both the public core boundary and the CLI catch unexpected validation/dependency
exceptions and return `REFUSE ci_pending facts unavailable or invalid`, without
exception text or a traceback. Malformed parser/coverage return values refuse
`seat_missing`. The CLI validates the decision's outcome, defined reason code,
single-line detail and exact GO head before printing exactly one result line.
The defined refusal codes remain `head_moved`, `ci_red`, `ci_pending`,
`seat_missing`, `seat_pinned`, `seat_family_is_writer`, `finding_open`,
`gate_path`, `tier_high`, `plan_key_mismatch`, `preflight_missing`, `stale_green`.

Evidence paths are literal: `*`, `?` and `[` refuse `gate_path` in PR paths or
`stale_green` in main-delta paths. Only imported door patterns may use globs.
Shared fixtures are registered in `tests/conftest.py`, with bounded support code
in `tests/_merge_gate_support.py`; neither test module imports the other. The
guards block subprocess entry points, `os.system`/`os.popen`, socket construction
and connection/server helpers, `urllib.request.urlopen` and verdict posting.
Real dependency calls retain the scoped file/environment/clock guards. Tests
isolate each live door table and verify the refusal disappears without it.
