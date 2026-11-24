# Session lifecycle: stop at DONE, reap idle

`pg-session-resume` (rust/pg-session-resume) closes agent sessions by code and records
how to resume each one. Nobody types `claude stop`. Later slices of SESSION-AUTO-CLOSE add
the findings contract, the delivery of findings and the resume itself; this page holds what
exists.

## Launch record

`<state>/launch/sessions/<session_name>.json` (state = `$ROUTING_STATE_DIR`, default
`~/.local/state/routing`). The launcher owns the launch fields; the tool keeps every unknown
field and writes only the stop fields below, in one atomic rename. It reads `cwd`, `ts`,
`name`, `session_name`, `lane` (else the last component of `cwd`), `agent` (`claude` default,
`codex`, `opencode`) and the handle: `session_id` (Claude; the job id is `job_id`),
`thread_id` (Codex), `opencode_session` (OpenCode).

The lane status file `$PG_PR_DIR/<lane>.status` (default `~/.local/state/pr`) is the
batch-writer STATUS_FILE: a line `<lane> DONE` and a line `HEAD: <40 hex>`. The last `HEAD`
line decides; a malformed one clears an earlier valid one.

## Stop

A stop needs the runtime's own positive idle signal:

- Claude: `~/.claude/jobs/<job id>/state.json` says `state: done` with no running tasks
  (`working`, `blocked` and a job with tasks are kept; `stopped` only gets its record
  written; an unreadable state keeps the session and logs once). The count is `inFlight`
  tasks + queued + drainableMonitors; a count that is missing or not a number is
  `job-tasks-unknown`, never zero. Activity is the newest of the transcript and state.json mtimes.
- Codex and OpenCode: `pid` and `pid_start` (field 22 of `/proc/<pid>/stat`; a pid with
  another start time is never signalled, a process confirmed gone counts as already stopped,
  an unreadable `/proc` entry keeps the session as `no-runtime-state`), no child
  process, and `turn_end_path`, a file the runtime or launcher touches when a turn ends: its
  mtime must be strictly newer than the activity (nanosecond mtimes; the same instant or the
  same second proves nothing). Without it the session is kept (`no-idle-signal`). Activity is
  `activity_path` (a per-session file the launcher names), else the Codex rollout file;
  OpenCode without `activity_path` is never reaped as idle.

Codex/OpenCode are stopped with SIGTERM through a pidfd opened for the recorded pid and
re-checked against `pid_start`, so a reused pid is never hit, and `stopped` is published only
after the whole process is confirmed gone through the pidfd (10 s; a survivor logs `stop-failed`
and the record stays unmarked). A zombie leader whose other threads still run is a running
process, not a gone one.

A kept session logs its reason once per cause (`stop_refused` in the record, set only after the
log line was appended, so a failed append is retried by the next tick): `no-job-state`,
`job-<state>`, `job-has-tasks`, `job-tasks-unknown`, `no-process-identity`, `no-idle-signal`,
`turn-open`, `has-children`, `no-runtime-state`, `no-resume-handle`. Never stopped: a session
without a handle, and names matching (case-insensitively) the built-in globs `*-L1`, `*-L1-*`,
`L1`, `L1-*`, `routing*`, `plan-*`, `overseer*`, `*planner*`, `*judge*`, `plangraph-oversight*`
(a superset of the Python reaper's list) or a line of `<state>/launch/stop-allowlist.txt`.

A stop writes `state: "stopped"`, `stopped_ts` and `resume: {agent, handle, head (git HEAD at
the stop), worktree (inode and git dir), status_head, round, stopped_ts, reason}`; head and
worktree stay null when their lookup failed. `resume.handle` is the "resumable" mark; the lane
gets `<lane>.resume.json` with the same handle. A stop belongs to the launch that produced it (`resume.launch_ts`, `launch_run`, `handle`
must equal the record's `ts`, `run_id` and handle), so a `fleet-launch --respawn` copy of the
record is a live session again. Before a session is touched, the snapshot of the stop (head,
worktree, status head, round, reason, time) is saved in the record as `stop_pending`; a stop
whose record write then fails exits 2, and the next tick, once the runtime shows the session
stopped, publishes from that snapshot without a second stop. A session that survives the stop
leaves no snapshot. Each lane keeps the copy of its latest stopped launch: a missing or older
copy is rewritten, a newer one is left. A kernel lock (flock on
`<state>/launch/pg-session-resume.lock`) keeps two ticks apart: a second one prints that the
lock is held and leaves the work to the first.

Commands:

- `stop-done`: stop a live session whose lane status says DONE with a valid `HEAD`, quiet for
  `--quiet-s` (60); `--session <id>` restricts it to one session, `--delay-s` waits first.
- `reap`: stop a live session idle for `--idle-min` (30). `python -m plangraph.session_reap`
  stays the reaper for Claude jobs without a record.
- `tick`: `stop-done` then `reap`.
- `stop-hook`: the Claude Stop hook; reads the hook JSON on stdin and starts a detached
  `stop-done --session <id> --quiet-s 0 --delay-s 3`.

Wiring (not enabled by this change): `deploy/systemd/pg-session-resume.{service,timer}` run
`tick` every minute; enabling a timer or registering the Stop hook needs a routing D-id.

## Log

One line per event, appended to `<state>/session-lifecycle.log`:
`ts=<unix> event=stop|stop-skipped|stop-failed name=<session_name> age_s=<since launch>
reason=done|idle ram_freed_mb=<MemAvailable delta or unknown> handle=<id>` (`stop-skipped` and
`stop-failed` carry `why=` or `error=` instead of the last fields). A line that cannot be
appended makes the run exit 2.
