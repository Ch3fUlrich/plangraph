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
| `<lane>.fix-round-<N>.txt` | the orchestrator | the fix-round prompt, N >= 1, no leading zeros |

A findings file is the prompt: its whole text is sent as one argument, byte for byte
(no header, no wrapping). Write it atomically (temp file, then rename); an empty or
whitespace-only file is ignored, one over 100000 bytes is refused once and logged.
A file that already exists when a session is stopped counts as delivered. A new
review writes the next round number; only the highest unconsumed round is sent.

## Launch record

`<state>/launch/sessions/<session_name>.json` (state = `$ROUTING_STATE_DIR`, default
`~/.local/state/routing`). The tool keeps every unknown field. It reads `cwd`, `ts`,
`name`, `session_name`, `lane`, `agent` (`claude` default, `codex`, `opencode`) and the
handle: `session_id` (Claude; the job id is `job_id`), `thread_id` (Codex),
`opencode_session` (OpenCode); Codex and OpenCode also need `pid` to be stopped.

A stop writes `state: "stopped"`, `stopped_ts` and
`resume: {agent, handle, head, round, stopped_ts, reason}`. `resume.handle` is the
"resumable" mark; the feeder counts a record with it as resumable, not running.
A resume clears `stopped_ts`, sets `state: "running"` and `resume.round` to the
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
- `reap`: stop a live session idle for `--idle-min` (30) with no unconsumed findings.
  Never stopped: names matching the built-in globs (`*-L1`, `*-L1-*`, `routing`,
  `plan-*`, `overseer`, `planner`, with `-*` forms) or a line of
  `<state>/launch/stop-allowlist.txt`.
- `tick`: the three in that order. `stop-hook`: the Claude Stop hook; reads the hook
  JSON on stdin and starts a detached `stop-done --session <id> --quiet-s 0 --delay-s 3`.

## Log

One line per event, appended to `<state>/session-lifecycle.log`:
`ts=<unix> event=stop|resume|stop-skipped|stop-failed|resume-deferred|resume-refused|resume-failed name=<session_name> age_s=<since launch> reason=done|idle|findings ram_freed_mb=<MemAvailable delta, or unknown> handle=<id>`.
