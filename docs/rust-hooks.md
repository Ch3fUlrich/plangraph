# Rust hook shadow harness (RUST-HOOKS slice 1)

Run `pg-hook shadow ENTRY LOG_PATH PYTHON_HOOK_PATH` with the original payload on stdin.
Python runs without added interpreter flags, preserving sibling imports, `PYTHONPATH`
and user-site behavior. `PG_HOOK_PYTHON` can select an absolute interpreter path;
relative or empty overrides are refused with `PG_HOOK_PYTHON must be an absolute path`,
a best-effort JSONL harness fault and exit 0. Otherwise the default is `python3`.
RUST-HOOKS slice 1 is Unix-only; non-Unix test branches are gated with `cfg(unix)`.
Python's exit code, stdout and stderr remain authoritative; Rust errors, unwind panics
and log-write failures cannot change that result. Logs append JSONL `{entry, same, py, rust}`;
successful outputs use the existing parity fields `exit`, `stdout`, `stderr`.
Both implementations receive identical raw stdin bytes, even for invalid UTF-8.
CLI usage mistakes and internal failures (including stdin reads or Python spawn)
fail open with exit 0; an executed Python hook's status still wins. On Unix, signals
are encoded as 128 + signal in both the returned status and the parity comparison;
other platforms retain their native exit codes, without claiming Unix signal parity.
If the platform provides neither a code nor a Unix signal, `py.exit` and `same` are
`null` and the CLI fails open with exit 0, never inventing a comparable exit code.
Rust failures use `error` or `panic`; pending hooks use `status: NotImplemented`, not `error`.
All five registry entries are pending: `bash_guard`, `fleet_inject_pre`, `fleet_inject_post`,
`fleet_report`, `ctx_guard_session`. Supply the matching Python script explicitly
(`fleet_inject.py` serves both inject entries). No settings or switch wiring is installed.
Harness faults add `harness: {error}` and `same: null` rather than a false parity
comparison; unavailable Python/Rust observations are `null`. Spawn, stdin read/write,
wait and harness unwind failures are accounted for when the log is writable.
Pending, failed and panicking Rust hooks have no decision: their status/error/panic
details remain in `rust`, but `same` is `null`, never a manufactured mismatch.
An early Python stdin close still preserves its completed output and status.
Only a valid shadow invocation with a known entry and nonempty log path can arm
the panic log; malformed arguments cannot select a fault-record destination.
Logs contain hook outputs: on Unix the harness creates them with mode 0600 and
tightens existing log permissions before appending. Log paths are explicit arguments.
`cd rust && cargo test -p pg-hook` runs isolated replay tests and loads the real
`tests/hooks/parity_payloads.json`: recorded Python outputs are the differential oracle.
The driver keeps `{TOK}` symbolic (no secret-shaped strings are constructed),
substitutes `{TMP}`, seeds temporary files/HOME/state,
maps historical `fleet_inject`/`context_guard` names, and skips absent Rust decisions
(including NotImplemented, errors and panics), consistent with production JSONL.
The driver explicitly checks that the pending registry produces no comparisons,
then injects no-op test hooks for the 11 recorded no-op report/session cases and a
different-output hook for 12 negative comparisons. Both inject aliases also run
through independent no-op test hooks: all six inject cases produce comparisons,
including two intentional seeded-inbox mismatches and four matches. Together these
exercise every one of the 29 corpus cases and the comparator
without claiming that pending production hooks have passed parity. Replay tests
resolve Python before clearing the environment; override CLI tests select that absolute
interpreter in place with no PATH. The default-interpreter import regression uses a
temporary Unix PATH containing only a symlink to the resolved interpreter.
Replay helpers use `-I`; CLI tests clear the environment and use temporary HOME/state.
The isolated-interpreter CLI regression supplies `-I` through a test-only Unix shim,
never through production arguments. Import regressions explicitly seed sibling and
`PYTHONPATH` modules. Echo fixtures use binary streams. Windows corpus substitution
and interpreter discovery are deferred: there is no verified `pg-hook` Windows test
result at the reviewed head, so the `rust-windows` job does not run `cargo test -p pg-hook`.
Only `rust/pg-hook/**` maps to this doc; the shared workspace manifest and lock
file remain unmapped, as required by `.docs-map`'s shared-file policy.
Merge policy tests retain the unique crate-local mapping and its existing doc,
and preserve all workspace members in main's path order, including `pg-reap`.
Hook ports, property tests, 14-day zero-diff observation, rollback canary and settings
integration remain deferred; the production switch belongs to **RUST-HOOKS-SWITCH**.

## Git commit messages

Run `tools/hooks/install_hooks.sh --git` with the host's shared
`CARGO_TARGET_DIR` set. It builds the Rust `commit-msg` executable, installs it
into the checkout's `tools/hooks`, and sets worktree-scoped `core.hooksPath` to that directory
(including its existing pre-push). Re-run after changing the Rust entry point or validator.
The installer snapshots `tools/ci_commit_gate.py` into the ignored
`tools/hooks/.commit-msg-gate`; the binary calls that installed validator: both CI and Git use `message_violations` for Writer, strict Hand-Edit,
and `Changelog: skip (<reason>)` validation. An active merge (`MERGE_HEAD`) bypasses the hook.
Missing files, interpreters, and failed Git probes refuse the commit.
Git editor comments are stripped for validation with `git stripspace --strip-comments`
(respecting `core.commentChar` and `core.commentString`); the original message file stays intact.
Verbose editor scissors sections are excluded too.

Git does not tell commit-msg whether the invocation is `--amend`. Once
`MERGE_HEAD` is gone, amending a trailerless merge requires adding attribution
or using Git's explicit `--no-verify` override. Inferring an amendment from
HEAD or matching messages would also exempt new non-merge commits after a merge;
the hook keeps those guarded. CI still exempts all actual merge objects.

The installer enables `extensions.worktreeConfig` and writes only the current
worktree's hook path. Removing that worktree leaves other worktrees' hooks intact.
The existing `check_push_gate.py --install` accepts the equivalent absolute path
and also preserves worktree scope. Scissors detection supports Git's automatic
comment-character selection.

The validator snapshot survives branch switches to revisions without the new
CLI subcommand. It is generated from the shared source at installation, never
maintained as a second policy implementation. The hook path is relative within
the worktree, so moving a worktree preserves its hooks. No-editor commits keep
comment lines under Git's default cleanup; editor `auto` prefixes are inferred
from Git's generated comment block when no scissors line exists.

The commit-msg binary reads the installed validator's trailer block and value
violations through a JSON bridge. It accepts policy trailers followed by a separate
attribution paragraph, using the same maximal trailing trailer-shaped paragraphs
as CI and the changelog/docs checker. A `Plan-Item`, `Writer`, `Hand-Edit`,
`Changelog`, or `Gate-Edit` key outside that block refuses the commit and names
its line in the cleaned message. Value validation still runs, so misplaced keys
cannot hide malformed attribution or skip reasons.

## Pre-push tree isolation

Pre-push measures every pushed head with `check_file_size.py --head`, including
that tree's `.filesize-allow` and `.filesize-baseline`. Scrub reads committed
blobs and the public rules from each pushed head; private word lists and explicit
operator rules remain external configuration. The pushing checkout's files do
not grant exemptions or create file-size failures for another branch.

Preflight runs in a temporary detached worktree of each pushed head, including
when the pushing checkout has dirty files at that same revision. Git's local
hook environment is removed for the child so repository discovery selects the
temporary worktree. Cleanup runs after successful checks and failures. The
pushing checkout's Python interpreter remains available to launch preflight.
Rust regressions in `pg-hook`'s `pushed_tree` integration target exercise these
paths by feeding the real hook Git's ref records from isolated repositories.
