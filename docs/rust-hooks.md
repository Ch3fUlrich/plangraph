# Rust hook shadow harness (RUST-HOOKS slice 1)

The crate also provides the independent `checkpoint-size BASE HEAD` packaging
gate. Run `cargo run --manifest-path rust/Cargo.toml -p pg-hook --bin
checkpoint-size -- BASE HEAD` from the checkout root. It reports committed PR
diff bytes and file count and refuses at 100,000 bytes or above or over 25 files
(exit 1; measurement errors exit 2). The phase-batch skill requires splitting
oversized packages into stacked checkpoints before pushing or launching seats.
See [budget, evidence and remaining experiment work](checkpoints/checkpoint-size.md).
This command is separate from the shadow harness and does not install a hook.

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
resolve Python with the Rust test helper before clearing the environment. Override CLI
tests select that absolute interpreter in place with no PATH. The default-interpreter import regression uses a
temporary Unix PATH containing only a symlink to the resolved interpreter.
Replay helpers use `-I`; CLI tests clear the environment and use temporary HOME/state.
The commit-message fixture tests drop every inherited `GIT_*` variable so they cannot act on the hook's own repository.
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

## Session guards

Run `tools/hooks/install_hooks.sh --session-rust` with the shared
`CARGO_TARGET_DIR` set. The installer adds `pg-hook` to the hook directory
and its checksum manifest. Register the guarded commands printed by the
installer's `--help` in the harness settings.

### Lesson Stop hook

Register `pg-hook lesson-stop` for `Stop`, including continuation Stop
events. It reads the session transcript and exits 2 while it finds unrecorded
signals. The diagnostic gives each signal's ID and kind. Transcript content
stays local.

Record resolutions in `lesson-<session_id>.tsv` under
`PG_LESSON_LEDGER_DIR`. The default directory is
`$XDG_STATE_HOME/plangraph/lessons`, falling back to
`$HOME/.local/state/plangraph/lessons`. Each row contains the signal ID, a tab,
and either a new keeper LE key or `attached to <key>` for an existing item.
For example, pair `tool:tool1` with `plangraph:LE-COMMAND`. Each entry resolves
its own signal. Message IDs use transcript line and content-block positions.
Keep the ledger outside the repository.

The hook recognizes Claude and Codex tool failures and Claude teammate
messages with a leading `BLOCKED`, `FAIL` or `CORRECTION` status. Start
operator corrections with `Correction:` to record them as signals in either
transcript format.

### Routine messages to routing and stale session names

Register `pg-hook message-route` for `PreToolUse` on `SendMessage`, next to
`message-time`. A message to the routing role (`routing` or `routing-NNNN`)
whose first word is `ACK`, `DONE`, `FILED`, `MERGED` or `STATUS` (upper case) is
refused with exit 2 and appended as one line `- <UTC time> <sender>: <text>` to
`$ROUTING_STATE_DIR/inbox/L0.md` (default `~/.local/state/routing/inbox/L0.md`),
so routing reads it at its heartbeat instead of waking. Each diversion also adds
a row `{ts, day, sender, to, word}` to `inbox/diversions.jsonl`; count per day and
sender with `jq -r '[.day,.sender]|@tsv' inbox/diversions.jsonl | sort | uniq -c`.
`BLOCKED`, `GO-REQ`, `FAIL`, `INCIDENT` and operator relays are delivered. The
sender is the `session_name` of the launch record whose `session_id` matches.

A message to `<role>-NNNN` for `routing`, `plan-order` or `plan-roadmap`
(`PG_MESSAGE_SINGLETON_ROLES` replaces the list) is refused when
`launch/sessions/<role>-MMMM.json` exists with a higher counter; the refusal names
that live name. Writers are not singletons and are never refused this way.
The hook reads files only (under 50 ms) and fails open on any error. This host's
Claude settings must register the hook; a host without the entry gets no refusal.

### Agent message timestamps

Register `pg-hook message-time` for `PreToolUse` on `SendMessage` and the
shell tool. It checks the message field or a literal `fleet-msg send`
invocation. The fleet client also checks the resulting message before it loads
the transport for CLI sends and MCP `fleet_msg_send`. It finds the
default installed binary automatically. Set `PG_MESSAGE_TIME_HOOK` to an
absolute executable path for a custom install.

The guard checks `HH:MMZ` and `HH:MM:SSZ` against the current UTC date.
ISO timestamps carry their own date and offset. The guard allows one minute
beyond the current clock. Prefix approximate times with `about`, as in
`about 04:0xZ`. A refusal identifies the timestamp and exits 2.

The `SendMessage` tool is only covered where the host's Claude settings register
the hook; the fleet client and `fleet_msg_send` check the text themselves. Without
the settings entry and an installed `pg-hook` a guessed stamp such as `05:4xZ`
goes through unchecked. Entry, per hook (`message-time`, `message-route`):
`{"matcher": "SendMessage", "hooks": [{"type": "command", "command": "sh -c 'test -x \"$1\" || exit 1; exec \"$1\" message-time' _ ~/.local/share/plangraph/hooks/pg-hook"}]}`
under `hooks.PreToolUse`.

Read git times as UTC: `git log --date=iso-strict` or `TZ=UTC git log --date=format-local:%H:%MZ`.
A bare `--date=format:` prints the committer's offset (`+01:00`), which is not `Z`.

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

Rust hook tests construct Git commands through `pg_worker_core::git_test_support` so pre-push repository redirects cannot affect fixture init, config or commits. Fixture commands refuse the source repository and its linked worktrees.

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

The `guard-hook` binary wraps effective Claude Code PreToolUse guards with
a deadline and one incident record per outage. Build and wire it after a
scoped deployment GO. Its regression test reads the versioned effective
settings and exercises the Bash and context outage alerts. The fixture execution
runs in the Unix hook test suite.
[Fail-closed registration](guard-hooks.md)

The hook test suite publishes fake executables with the shared Rust fixture
writer so parallel process launches cannot race a direct script write.
