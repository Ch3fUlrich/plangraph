# Session lifecycle: stop at DONE, resume on findings, reap idle

`pg-session-resume` (rust/pg-session-resume) closes agent sessions by code and
resumes the same session when a review sends findings. Nobody types `claude stop`
or opens a new writer for a fix round.

## Files (the contract)

All lane files live in one directory: `$PG_PR_DIR`, default `~/.local/state/pr`.
`<lane>` is the `lane` field of the launch record, else the last component of its `cwd`.

| File | Writer | Content |
|---|---|---|
| `<lane>.status` | the batch writer | the batch-writer STATUS_FILE: a line `<lane> DONE` and a line `HEAD: <40 hex>` |
| `<lane>.fix-round-<N>.head` | the orchestrator | the 40-hex head the review of round N covered, one line; written first |
| `<lane>.fix-round-<N>.txt` | the orchestrator | the fix-round prompt, N >= 1, no leading zeros; written last |

A findings file is the prompt: its whole text is sent as one argument, byte for byte
(no header, no wrapping). Write it atomically (temp file, then rename); an empty or
whitespace-only file is ignored, one over 100000 bytes is refused once and logged.
A new review writes the next round number; only the highest unconsumed round is sent.
The `.head` sidecar binds the round to the head it reviewed: a round whose sidecar is
missing, or names another head than the one the session was stopped at (`resume.status_head`,
else `resume.head`), is refused once and logged (`no-reviewed-head`, `head-mismatch`); the
prompt text is never changed. Delivered means: the file is strictly older (nanosecond
mtime) than the moment the session stopped being the reviewer's (the mtime of the DONE
status file; for an idle stop, the last activity), or its round was already sent. A file
from the same instant counts as not delivered: a repeated prompt is cheaper than a lost
one. A findings file written after the DONE line, even before the stop ran, is sent. The lane also gets `<lane>.resume.json` (state `stopped|running`,
agent, handle, head, round), the lane-side copy of the launch record's `resume` object.

## Launch record

`<state>/launch/sessions/<session_name>.json` (state = `$ROUTING_STATE_DIR`, default
`~/.local/state/routing`). The tool keeps every unknown field. It reads `cwd`, `ts`,
`name`, `session_name`, `lane`, `agent` (`claude` default, `codex`, `opencode`) and the
handle: `session_id` (Claude; the job id is `job_id`), `thread_id` (Codex),
`opencode_session` (OpenCode). Codex and OpenCode also need `pid` and `pid_start` (field 22
of `/proc/<pid>/stat`) to be stopped: a pid whose start time differs belongs to another
process and is never signalled, a gone process counts as already stopped. A resume stores
the new process's pid and pid_start. A Claude job is stopped only when its
`~/.claude/jobs/<job id>/state.json` says `state: done` with no running tasks (`working`,
`blocked` and a job with tasks are kept; `stopped` only gets its record written; an
unreadable state keeps the session and logs once). A Codex or OpenCode process is stopped only
when it has no child processes (a child is a running tool); an unreadable process table keeps it. `activity_path` in the record names the
file whose mtime is the activity; else Claude uses the transcript and state.json mtimes,
Codex its rollout file, and OpenCode the shared OpenCode database (a conservative signal: a
busy neighbour keeps the session alive). A session with findings it has not received yet is
not reaped.

A stop writes `state: "stopped"`, `stopped_ts` and
`resume: {agent, handle, head (git HEAD at the stop), worktree (inode and git dir), status_head, round, stopped_ts, reason}`;
head and worktree stay null when their lookup failed, never replaced by the status HEAD.
`resume.handle` is the "resumable" mark a feeder reads (the feeder change belongs to the
feeder lane; this tool only publishes the mark in the record and `<lane>.resume.json`).
Only the newest stopped record of a lane resumes, never while another record of the lane
is live, and never when the status file now names another head than `resume.status_head`
(refused once, logged). A kernel lock (flock on `<state>/launch/pg-session-resume.lock`) keeps two ticks apart. The
round is first claimed (`resume.claim_round`, `claim_ts`), not delivered; delivery
(`resume.round`) is saved after the launch ends. A claim found by a later tick belongs to a
dead one: runtime activity after `claim_ts` means the launch happened (delivered), otherwise
the claim is dropped and the round is sent again (`event=resume-reconciled`). A resume clears `stopped_ts`, sets `state: "running"` and `resume.round` to the
consumed round. A session without a handle is never stopped.

## Commands

- `stop-done`: stop a live session whose lane status says DONE with a `HEAD` it was not
  already stopped at (a resumed writer keeps the old DONE line until it rewrites it).
  The transcript must be still for `--quiet-s` (60).
- `resume`: for a stopped record and a findings round above `resume.round`, run
  `claude --bg --resume <id> <text>` (Codex `codex exec resume <id> <text>`, OpenCode
  `opencode run --session <id> <text>`) in the record's `cwd`. Free RAM below
  `--min-free-mb` (4096) defers it. A dirty worktree or a HEAD other than `resume.head`
  does not block the resume; the log line says `worktree=dirty`, `moved` or `dirty+moved`.
- `reap`: stop a live session idle for `--idle-min` (30). It covers the sessions that have a
  launch record and records their handle; `python -m plangraph.session_reap` stays the
  reaper for Claude jobs without a record.
  Never stopped: names matching the built-in globs (`*-L1`, `*-L1-*`, `routing`,
  `plan-*`, `overseer`, `planner`, with `-*` forms) or a line of
  `<state>/launch/stop-allowlist.txt`.
- `tick`: the three in that order. `stop-hook`: the Claude Stop hook; reads the hook
  JSON on stdin and starts a detached `stop-done --session <id> --quiet-s 0 --delay-s 3`.

## Wiring

- Timer: `deploy/systemd/pg-session-resume.{service,timer}` run `pg-session-resume tick`
  every minute (`systemctl --user enable --now pg-session-resume.timer`). The service uses
  `KillMode=process` and a resumed Codex/OpenCode writer starts in its own process group, so
  the oneshot's exit does not kill it.
- Launcher (the session launcher that writes the launch records; a recorded slice): write `lane`, `agent`, the
  Codex `thread_id` / OpenCode `opencode_session`, `pid` and `pid_start`, and for OpenCode an
  `activity_path`. Claude records already work as they are.
- Orchestrator: write `<lane>.fix-round-<N>.head` before the `.txt`.
- Stop hook: register `sh -c 'test -x "$1" || exit 1; exec "$1" stop-hook' _ <path>/pg-session-resume`
  on the Claude Code `Stop` event. Without it the timer stops a session about a minute
  after its DONE line.
- Both are persistent services or permission settings: enabling them on a host needs a
  routing D-id.

## Log

One line per event, appended to `<state>/session-lifecycle.log`:
`ts=<unix> event=stop|resume|stop-skipped|stop-failed|resume-deferred|resume-refused|resume-failed name=<session_name> age_s=<since launch> reason=done|idle|findings ram_freed_mb=<MemAvailable delta at a stop, 0 at a resume, or unknown> handle=<id>`. A resume line also has `ram_free_mb` (MemAvailable then) and `worktree=clean|dirty|unknown`, plus `+moved`, `+replaced` (another checkout at the same path) and `+head-unknown`, `+worktree-unknown` (no baseline). A line that cannot be appended makes the run exit 2.
