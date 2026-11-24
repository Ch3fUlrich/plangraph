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

A findings file is the prompt: its whole text is sent as one argument, byte for byte (no
header, no wrapping). Producers call `pg-session-resume deliver --lane <lane> --head <sha>
[--file F]` (text from F or stdin): under a per-lane lock it takes the next round number, writes
the `.head` sidecar and then the `.txt`, each by rename, so concurrent producers never share a
round or pair one review's head with another's text. An empty or whitespace-only file is ignored; one
over 100000 bytes is refused once and logged. Only the highest unacknowledged round is sent.

The sidecar binds a round to the head it reviewed. A round is sent only when its sidecar names
the head the session was stopped at (`resume.status_head`, else `resume.head`) and the lane
status still names that head. Otherwise it is refused once and logged: `no-reviewed-head`,
`head-mismatch`, `head-superseded`, `no-stop-head`, `no-lane-head` (the lane status is missing,
unreadable or has no valid HEAD: nothing is sent without a verified current head). No clock decides anything: a round is
delivered when the runtime acknowledges it, and `resume.round` is the last acknowledged round.
The lane also gets `<lane>.resume.json` (state `stopped|running`, agent, handle, head, round),
the lane-side copy of the launch record's `resume` object.

## Launch record

`<state>/launch/sessions/<session_name>.json` (state = `$ROUTING_STATE_DIR`, default
`~/.local/state/routing`). The tool keeps every unknown field. It reads `cwd`, `ts`, `name`,
`session_name`, `lane`, `agent` (`claude` default, `codex`, `opencode`) and the handle:
`session_id` (Claude; the job id is `job_id`), `thread_id` (Codex), `opencode_session`
(OpenCode).

Stopping needs the runtime's own positive idle signal:
- Claude: `~/.claude/jobs/<job id>/state.json` says `state: done` with no running tasks
  (`working`, `blocked` and a job with tasks are kept; `stopped` only gets its record written;
  an unreadable state keeps the session and logs once). Activity is the newest of the transcript
  and state.json mtimes.
- Codex and OpenCode: `pid` and `pid_start` (field 22 of `/proc/<pid>/stat`; a pid with another
  start time is never signalled, a gone process counts as already stopped), no child process,
  and `turn_end_path`, a file the runtime or launcher touches when a turn ends: its mtime must
  be strictly newer than the activity (nanosecond mtimes; the same instant or the same second
  proves nothing). Without it the session is kept (`no-idle-signal`). Activity
  is `activity_path` (a per-session file the launcher names), else the Codex rollout file;
  OpenCode has no other per-session source, so without `activity_path` it is never reaped as idle.

A stop writes `state: "stopped"`, `stopped_ts` and `resume: {agent, handle, head (git HEAD at
the stop), worktree (inode and git dir), status_head, round, stopped_ts, reason}`; head and
worktree stay null when their lookup failed. `resume.handle` is the "resumable" mark a feeder
reads (the feeder change belongs to the feeder lane; this tool publishes the mark in the record
and `<lane>.resume.json`). A session with a round for its current head that it has not
acknowledged is not reaped. A session without a handle is never stopped.

Resume: only the newest stopped record of a lane resumes, never while another record of the lane
is live. A kernel lock (flock on `<state>/launch/pg-session-resume.lock`) keeps two ticks
apart. The round is claimed first (`resume.claim_round`, `claim_ts`, `claim_offset` = transcript
length, `claim_pid`, `claim_start`), the launch runs, and the claim is settled against the
runtime: delivered when the transcript holds the prompt after `claim_offset` (a user message after `claim_offset` holds the prompt; an assistant message quoting it does not
count; a runtime without a transcript: the process this launch started is alive (a zombie is
gone) and carries the prompt in its own arguments). A
delivery sets `resume.round`, `state: "running"`, clears `stopped_ts` and the claim, keeps the
verified launch identity (pid and start time, found again from its command line after a dead
tick) even when the process already exited, so the next stop records it as gone and logs
`event=resume-acked`. A claim not acknowledged with nothing running, or past 120 s without a
process, is dropped, the record is stopped again and the round is sent again
(`event=resume-reconciled outcome=not-acked`).

## Commands

- `stop-done`: stop a live session whose lane status says DONE with a `HEAD` it was not
  already stopped at (a resumed writer keeps the old DONE line until it rewrites it).
  The session must be quiet for `--quiet-s` (60).
- `resume`: for a stopped record and an unacknowledged findings round for its head, run
  `claude --bg --resume <id> <text>` (Codex `codex exec resume <id> <text>`, OpenCode
  `opencode run --session <id> <text>`) in the record's `cwd`. Free RAM below
  `--min-free-mb` (4096) defers it. A dirty worktree or a moved HEAD does not block the
  resume; the log line says so.
- `deliver`: the findings producer above.
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
- Launcher (the session launcher that writes the launch records; a recorded slice): write `lane`,
  `agent`, the Codex `thread_id` / OpenCode `opencode_session`, `pid`, `pid_start`, and for
  Codex/OpenCode `activity_path` and `turn_end_path`. Claude records already work as they are.
- Orchestrator: hand a fix round over with `pg-session-resume deliver` (a recorded slice).
- Tests: `tests/systemd.rs` runs the deployed `tick` as a transient systemd user unit with the
  deployed `KillMode` and checks the resumed writer is still alive (not a zombie) afterwards and
  got the text; with `KillMode=control-group` it fails.
- Stop hook: register `sh -c 'test -x "$1" || exit 1; exec "$1" stop-hook' _ <path>/pg-session-resume`
  on the Claude Code `Stop` event. Without it the timer stops a session about a minute
  after its DONE line.
- Both are persistent services or permission settings: enabling them on a host needs a
  routing D-id.

## Log

One line per event, appended to `<state>/session-lifecycle.log`:
`ts=<unix> event=stop|resume|stop-skipped|stop-failed|resume-deferred|resume-refused|resume-failed name=<session_name> age_s=<since launch> reason=done|idle|findings ram_freed_mb=<MemAvailable delta at a stop, 0 at a resume, or unknown> handle=<id>`. A resume line also has `ram_free_mb` (MemAvailable then) and `worktree=clean|dirty|unknown`, plus `+moved`, `+replaced` (another checkout at the same path) and `+head-unknown`, `+worktree-unknown` (no baseline). A line that cannot be appended makes the run exit 2.
