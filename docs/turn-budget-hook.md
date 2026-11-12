# Turn-budget hook (D-5212, D-5252), slice 1

`turn-budget-hook` is a standalone Rust Claude Code command hook. It accepts one
JSON object on stdin. Allowed calls are silent and exit 0; only a recognised
polling command exits 2 with one stderr line naming the replacement wait command.
Budgets report usage; exceeding a budget does not itself deny tools.

## Build and optional registration

Build with `cargo build --manifest-path rust/Cargo.toml -p pg-worker-core --bin
turn-budget-hook --release`. Copy `rust/target/release/turn-budget-hook` into your
chosen hook directory. Unlike the Python hooks next to `install_hooks.sh`, this
binary is not shipped by that Python-only copier. No automatic installation or
settings edits are part of this slice.

For a later operator-approved registration, follow the existing missing-hook
guard convention: `sh -c 'test -f "$1" || exit 1; exec "$1"' _
DEST/turn-budget-hook`. Replace `DEST` with the chosen install directory; the
missing-file case exits 1, never the tool-denial code 2. Register the same command
for `UserPromptSubmit`, `Notification`, `PreToolUse`, `PostToolUse` and `Stop`.
Pre/post matchers must include all tools so model responses with non-Bash tools
are counted; only Bash pre-tool events are candidates for polling refusals.

## State and accounting contract

- State root: `FLEET_STATE_DIR`, else `$HOME/.local/state/fleet`, matching the
  existing fleet hooks. The library's `Config` and integer `now` are injectable.
- Session name: `FLEET_SESSION_NAME`, else the matching `session_id` record under
  `FLEET_LAUNCH_DIR` (default `$HOME/.local/state/routing/launch`); if unresolved,
  use the safe session ID. Reject unsafe IDs before joining paths.
- Status: `<state>/status/<session_id>.json`, with counters under `turn_budget`.
  Existing unrelated top-level keys survive. The status includes `session`,
  `session_id`, `role`, `wake_calls`, `hour_calls`, recent `calls` timestamps,
  transcript cursor, seen API message IDs and hashed read-command history.
  Neither commands nor tool output are stored. A per-session nonblocking lock
  serializes updates; a temporary file in the same directory is flushed and
  atomically renamed so readers never see a partial JSON document. A busy lock
  skips the event rather than holding a tool call open.
  Replacement retains an existing status file's permissions; new files keep the
  private tempfile default. The file is synced before rename and, on Unix, the
  parent status directory is synced after rename for crash durability.
- One model call is one distinct `assistant.message.id` in the session's JSONL
  transcript. Multiple streamed blocks or tool uses from that response count
  once. User/tool-result, progress, sidechain and missing-ID records do not count.
  Hook events are **not** model-call proxies. `Stop` captures text-only responses.
- Read only newly appended complete transcript records; retry a partial final
  record on the next event. On truncation/path change replay with message-ID
  deduplication. Record timestamps determine the rolling hour `(now-3600, now]`;
  a missing/invalid timestamp uses the injected current time. Future timestamps
  are excluded from the hour. An hour with no events is recomputed by the reader.
- A wake begins at `UserPromptSubmit` or `Notification`. First ingest any unseen
  previous-wake responses, then reset `wake_calls` to zero; preserve hour history.
  `Stop`, pre/post-tool events, and different read commands do not reset the wake.
- `ROLE_BUDGETS` starts build/review L1/L2, planner and routing at 6 calls per wake
  and 60 per hour. Unknown roles use the same fallback. Remove only the final
  numeric suffix: `build-L1-0429` becomes `build-L1`, not `build`.

Malformed stdin, unreadable/corrupt state, counter failures and missing transcripts
never deny an ordinary tool. Input is capped at 8 MiB, status at 1 MiB and a
transcript delta at 8 MiB. These caps, a busy lock, or failed IO may undercount;
this is a fail-open advisory counter, not billing. Seen IDs are SHA-256 hashes
retained for the newest 2,048 distinct responses; replay outside that window can
count again. The newest 256 distinct read fingerprints and at most 16 hashed
path dependencies per read are retained; an evicted read may pass once again.
The hour retains at most 8,192 timestamps, so larger bursts undercount the hour
but continue incrementing the wake counter. Legacy histories below the file cap
are compacted on the next update, avoiding permanent growth-induced freezes.
Unrelated top-level fields still consume the shared 1 MiB cap and are never
discarded to recover space. Historical transcript replay
without a recorded wake is attributed to the current wake; register at session
start for precise boundaries. No live counts are provided here.
An existing status over 1 MiB is preserved byte-for-byte and cannot resume
accounting until repaired externally; historical read refusals are skipped too.
An update that would exceed the cap leaves the original file and directory
entries intact, without a stray temporary file. A denial already determined for
the current invocation still returns if persistence fails; later invocations
only have the last successfully persisted history, not an in-memory guarantee.

## Polling refusal

The Bash pre-tool check recognises foreground `while`/`until` loops containing a
sleep, and sleep-then-read chains separated by `;`, `&&`, or a newline. Literal
quoted prose and comments are not executable commands. Simple shell `-c`/`-lc`
wrappers are examined up to three levels deep. A loop does not become an allowed
wait merely by setting `run_in_background` or appending `&`.
Quoted or escaped shell keyword words are not treated as control flow.
Sleep-free `while`/`until` loops consisting only of recognised snapshots
and shell no-ops are also refused, as is `watch -n 5 git status` (including
supported interval/display options). Thus `while git status; do :; done` and
`while true; do env git status; done` are not loopholes. Mixed work loops such as
`while build_next; do git status; done` and `for i in 1 2 3; do touch marker;
git status; done` remain allowed by design rather than blocking ordinary work.
Self-terminating `for` lists are allowed, including `for f in *.md; do cat $f;
done`, `for i in 1 2 3; do cat notes.txt; done`, and fixed-list git reads or
sleep/read iterations. An enclosing `while`/`until` still makes a repeated
snapshot loop. Common literal `timeout`, `env`, `nice`, `nohup`, and `command`
wrappers are unwrapped, including nested shell `-c`/`-lc` reads: `timeout 2 git
status` passes once but shares the underlying snapshot history with `git status`.
Wrapper parsing supports timeout signal/kill options, env assignments/unsets and
ignore-environment, nice adjustment and command `-p`; unknown wrapper options
remain outside this narrow recogniser. Tests pin each of these boundaries.

The first recognised read-only check passes and records a SHA-256 fingerprint of
its parsed words plus `cwd` per session. Literal path operands are normalised
before hashing (`./status.json` and `status.json` share history); search patterns
and option values retain their spelling. Repeating a local check is refused, even
after other read commands or notifications. A successful non-read Bash command
grants exactly one retry of each local read, including after builds, scripts,
in-place editors and no-op commands; the next unchanged read is refused again.
The local refusal tells the caller to run the work that can change the result,
then retry once, rather than prescribing a spawner wait for a plain file read.
A successful
`PostToolUse` for Edit/Write/NotebookEdit, a recognised mutating Bash command, or
output redirection also supplies mutation evidence for reads whose observed paths can
plausibly be affected. File reads depend on their literal path arguments; git
snapshots depend on paths under their absolute `cwd` workspace, and directory
listings depend on their named subtrees. Remote gh/spawner checks cannot be
reset by local writes, but receive a comparison execution after their first
successful output. Only an identical successful comparison blocks subsequent
remote reads for 60 seconds from that result; a fresh comparison is then allowed
even if remote state changes without local writes. Changed remote output keeps
comparisons reachable. Missing output never proves an identical remote snapshot.
A pre-tool mutation or failed mutation does not reset a read; a failed read does
allow its own retry. This is evidence of a relevant mutating operation, not a filesystem diff.
For example, `touch marker` or `git add unrelated` cannot reenable remote
`gh pr checks 42`, but grants a bounded local retry for `cat status.json`;
`touch status.json` also supplies relevant mutation evidence for that file
read, while `touch marker` or `git add unrelated` in the same workspace can
reenable `git status` by design because they can affect its worktree/index view.
Unrelated Edit/Write paths and missing path metadata never grant scoped retries;
successful non-read Bash commands still grant the bounded local retry even when
their mutation paths cannot be resolved, including directory-changing compounds.
Successful read results hash only output fields
(`stdout`, `stderr`, `output`, `content`, or a string response); a differing
result also permits one retry for that read alone. Identical output does not.
Read results only advance history for the currently admitted pre-tool event;
the next pre-tool event expires all pending admissions, even when a post never
arrived. Standalone or duplicated posts without a current admission do not
advance history. This is sequential event matching, not a tool-call nonce:
out-of-order same-fingerprint results during a newer admission are not
distinguishable when the payload supplies no matching identity. Paths are platform-native absolute
paths; the tests use the same fixture contract on Unix and Windows.
For simple `cp`/`install`, only the destination is mutation evidence; naming the
observed file as a copy source is not scoped evidence, although successful
non-read Bash execution still grants the same one-shot local retry.

Recognised reads include ordinary git status/log/show/diff/rev-parse/ls-files,
gh PR checks/view/list/status, gh run view/list, spawner ps/status/result and
cat/head/tail/grep/rg/ls/stat/test/ps. Recognised mutations include file operations
(touch/mkdir/mv/cp/rm/install/tee), git writes, and output redirection. Unsupported
shell syntax, variable expansion, aliases, heredocs and arbitrary custom checks
are not a sandbox: they fail open rather than being guessed read-only. The
existing security-focused `bash_guard.py` remains independent and unchanged.
Simple variable arguments do not hide an otherwise recognised sleep loop or
sleep-then-read chain. The loop/sleep scan runs on the original command independently
of read/scope parsing: substitutions, backticks, redirects, unbalanced quotes and
variable indirection do not disable recognised polling refusals within the depth limits below. Specifically,
`while true; do sleep 2; git status $(echo); done` and `sleep 5; git status $(true)`
are refused, as are snapshot loops with those syntax additions. Repeated ordinary
`git status $(date)` (or backticks) and `echo "$(cat state)"` still pass because
unsupported non-loop reads are not guessed read-only or added to history.
Variable command/subcommand indirection (`tool=git; $tool status`, `$tool status`,
`git $subcommand`) and dynamic read arguments (`cat "$status"`) also remain
allowed across calls outside loops; indirect sleep-free loops are refused.
The raw polling scan starts at depth 0 and examines through depth 3 inclusive;
substitutions, backticks and shell `-c`/`-lc` bodies share this recursion budget.
Snapshot-command inspection also stops descending into shell bodies at depth 3.
Further nesting can pass unclassified: sleep/read inside four nested substitutions,
or a snapshot loop inside four nested shell bodies, is outside this static scan.
Tests pin recognition at depth 3 and fail-open behaviour at depth 4.
A `$`-leading command word counts as a poll check only inside a loop body;
`sleep 5; $SCRIPT` and `sleep 5; MODE=x $RUNNER` pass as unclassified work.
Literal assignment prefixes also do not hide snapshot loops. Regression tests
make these deliberate fail-open parser limits visible rather than claiming a
shell sandbox or treating ordinary substitutions as polling. Script-file contents
are not inspected: `bash poll.sh` and `./poll.sh` can contain sleep/poll loops
outside static detection. A disposable script-file regression explicitly pins
this known limit; use the documented wait command instead of hiding a loop in a file.
Fixed-list loops do not hide an inner `watch` or shell-wrapped infinite loop;
arithmetic `for ((;;))` snapshot loops are refused, not treated as finite lists.
An adjacent sleep-then-read chain outside the finite loop body is still refused.
Legacy remote history missing comparison metadata can still reach a comparison
after upgrade. Missing post events, like missing output, cannot establish an
identical remote result.

Use `python -m plangraph.spawner.cli wait <run_id> --timeout 3600` with Bash
`run_in_background=true`: one command that exits, then its completion notification.
An ordinary command or a single background wait passes. Static sleep-polling
refusal still works with malformed session metadata or unwritable state. Without
usable session state, historical repeated-read detection is necessarily skipped.
A recognised `gh pr checks --watch` also passes as an exiting wait, not a snapshot
poll. A sleep followed by a mutation and then a read is ordinary work, not a
sleep-then-check chain. Refused attempts still contribute to model-call counts.

## Watchdog and tests

`alert_line(status, now)` returns `None` at/below both caps, otherwise exactly one
line: `TURN-BUDGET build-L1 build-L1-0429 wake=7/6 hour=61/60`. The
`read_alerts(state_dir, now)` function reads only injected status files, skips bad
files and recomputes the rolling hour. FLOW-WATCHDOG opts into that reader only
with `PLANGRAPH_TURN_BUDGET_STATE_DIR`; see [Flow watchdog](flow-watchdog.md).
The alert identity is `(role, session)`, not the live counts: active, queued and
pending lines refresh in place without adding a new event each tick. Completed
delivery stays suppressed until recovery. `PLANGRAPH_TURN_BUDGET_NOW` optionally
injects Unix seconds into this reader for deterministic subprocess tests; absent
or invalid values use the system clock. Production deployments should leave it
unset so rolling-hour expiry advances normally.
Corrupt, unreadable or over-1-MiB pending events are preserved and skipped during refresh
and acknowledgement; independent queue routing and other alerts still advance.
Legacy duplicate budget events compact by identity, retaining the oldest event's
channel acknowledgements even when several identities are interleaved.

Run the four `turn_budget_*` integration suites listed in CONTRIBUTING.md. They
use disposable state, synthetic transcripts, explicit clocks and cleared child
environments. They cover stdin/exit contracts, both counters, hour expiry, wake
resets, message deduplication, partial transcripts, name resolution, status-key
preservation, malformed inputs, IO failure, polling and opt-in watchdog delivery.
The stdin contract includes a valid JSON payload over 8 MiB that exits silently
without creating state. Atomic-write checks assert the exact `.json` and `.lock`
directory entries (not a tempfile suffix), concurrent readers always parsing
complete JSON during updates, mode preservation and bounded-history
recovery; watchdog tests exercise changing counts, delivery and recovery using
an explicitly injected clock rather than wall time.
Storage tests also cover existing over-cap status and rejected over-cap writes.
The watchdog subprocess suite is Unix-only because its delivery fixtures use
`/bin/sh` and `/bin/cat`; opt-out children clear both budget environment variables.
