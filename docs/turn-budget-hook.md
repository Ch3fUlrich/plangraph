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
this is a fail-open advisory counter, not billing. Seen IDs stay for the session
lifetime; retention/compaction is future work. Historical transcript replay
without a recorded wake is attributed to the current wake; register at session
start for precise boundaries. No live counts are provided here.

## Polling refusal

The Bash pre-tool check recognises foreground `while`/`until` loops containing a
sleep, and sleep-then-read chains separated by `;`, `&&`, or a newline. Literal
quoted prose and comments are not executable commands. Simple shell `-c`/`-lc`
wrappers are examined up to three levels deep. A loop does not become an allowed
wait merely by setting `run_in_background` or appending `&`.
Quoted or escaped shell keyword words are not treated as control flow.

The first recognised read-only check passes and records a SHA-256 fingerprint of
its parsed words plus `cwd` per session. Repeating that check is refused, even
after other read commands, notifications, or no-op commands. A successful
`PostToolUse` for Edit/Write/NotebookEdit, a recognised mutating Bash command, or
output redirection clears the read history. A pre-tool mutation or failed command
does not. This is evidence of a mutating operation, not a filesystem diff.

Recognised reads include ordinary git status/log/show/diff/rev-parse/ls-files,
gh PR checks/view/list/status, gh run view/list, spawner ps/status/result and
cat/head/tail/grep/rg/ls/stat/test/ps. Recognised mutations include file operations
(touch/mkdir/mv/cp/rm/install/tee), git writes, and output redirection. Unsupported
shell syntax, variable expansion, aliases, heredocs and arbitrary custom checks
are not a sandbox: they fail open rather than being guessed read-only. The
existing security-focused `bash_guard.py` remains independent and unchanged.
Simple variable arguments do not hide an otherwise recognised sleep loop or
sleep-then-read chain; command substitutions remain outside this narrow parser.

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

Run the four `turn_budget_*` integration suites listed in CONTRIBUTING.md. They
use disposable state, synthetic transcripts, explicit clocks and cleared child
environments. They cover stdin/exit contracts, both counters, hour expiry, wake
resets, message deduplication, partial transcripts, name resolution, status-key
preservation, malformed inputs, IO failure, polling and opt-in watchdog delivery.
