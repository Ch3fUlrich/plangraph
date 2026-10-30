# Claude Code sessions

Five MCP tools drive real Claude Code sessions: `plangraph_session_launch`, `plangraph_session_stop`,
`plangraph_session_relaunch`, `plangraph_session_mark_ready`, `plangraph_session_list`. Subprocesses
are argv lists, never shell strings.

Everything invalid is refused with `ParameterRefused` before anything runs: name outside
`[A-Za-z0-9][A-Za-z0-9._-]{0,63}` or ending in `.ready`, cwd outside `cwd_roots`, model/effort/permission_mode missing from the
config lists, unlisted `mcp_config`, anything but exactly one of `prompt`/`prompt_file`, a blank
prompt or one starting with `-`, prompt/handoff outside `cwd_roots`, non-boolean `dry_run`,
`stop` of a session without a resolved id, or `relaunch` with neither a ready marker nor a `handoff`.

An old id is checked against `claude agents --json` before `launch` replaces a stopped record or a
relaunch proceeds: a poll that fails or returns garbage refuses the action (fail closed), and a
listed id outside `finished`/`stopped` refuses `launch` with "use relaunch" - in `dry_run` too, as
it only reads. Once the process has started, a record write failure still reports the started id.
A failure result carries a scrubbed `error` and the started `session_id` when known.

The config file is selected by `$PLANGRAPH_SESSIONS_CONFIG`, defaulting to
`~/.config/plangraph/sessions.json` when that env var is unset: `cwd_roots`, `models`, `efforts`,
`permission_modes`, `mcp_configs`, `style_text`, `compact_window`, `ctx_cap_tokens`. It fails
closed - an unknown key (including the removed `style_file`, `file_roots` and `windows_launcher`),
bad scalar or missing file refuses every action and there is no default model. A present key must
carry its type and may not be `null`: `style_text` a non-empty string, `compact_window` a digit
string, `ctx_cap_tokens` a positive int. When unset, `ctx_cap_tokens` defaults to 300000 and
`compact_window` to `330000`.

**Threat model.** The caller is the authenticated MCP principal. Other processes of the same OS
account are trusted - they can already run `claude` themselves - so same-account file swaps are out
of scope. The config is the trust anchor: on POSIX it must be a regular file (not a symlink) owned
by the current user with mode 0600 or stricter (no group/other bits), opened once with `O_NOFOLLOW`
and read from that fd. Windows applies no POSIX checks.

The config read is fail-closed on POSIX: a platform without `O_NOFOLLOW` refuses the config
outright, `os.open` with `O_NOFOLLOW` refuses symlinks and any open failure ("not a readable
regular non-symlink file"), and the fstat of that one descriptor must show a regular file owned by
the current user with mode 0600 or stricter. The bytes obeyed are read from that same fd, never
re-opened by name after the checks (a swapped path cannot substitute content). `prompt_file` and
`handoff` must resolve inside `cwd_roots` and be readable regular files. An `mcp_config` must match
one of the `mcp_configs` entries by exact resolved path and still be a regular file when the action
runs; entries get no further per-file checks.

State lives under `<spawner state>/sessions/`: a `<name>.json` record per session, `<name>.prompt.txt`
(0600), the `<name>.ready.json` marker (handoff, card), and `audit.log` - one scrubbed JSON line per
action with the argv (prompt length only); a dry run writes nothing and leaves no audit line. The
prompt file is stored byte-exact for relaunch; the env redactor applies to audit lines, records and
results. The record carries a `state`: `launching` (with the
redacted argv and `launched_ms`) is written before the process starts, ending `running` on a resolved
id, `stopped` on a non-zero exit, `unknown` after a timeout, OSError or unresolved id - `session_id` null, `stopped_ts` unset, so `plangraph_session_list` shows the possible orphan and
`plangraph_session_relaunch` can replace it. A successful launch removes a stale
`<name>.ready.json`. Every audit write is guarded: when `audit.log` cannot be written the action's
own outcome stands and the caller is warned with a `warning` field instead. `stop` on a stopped
record re-checks `claude agents --json` and stops a still-live id. Concurrent actions on one name
are serialised by a per-name lock.

`plangraph_session_mark_ready` writes the marker. Relaunch uses the `handoff` argument, else the
marker's handoff; with neither, the relaunch is refused. The freshness check (handoff mtime strictly
newer than the last successful launch, `launched_ts`) and path containment always apply, and the
record's old handoff is never reused.
The stored launch spec is re-validated before the old session is stopped (`claude stop`, never
`rm`/`kill`), the previous session id is excluded when resolving the new one (UUID in launcher
stdout, else a short id on a `background` row of the same name, else the newest `background` row by
name + startedAt near launch), and a post-stop launch failure keeps the record and marker for a
retry. When the record is already marked
stopped but still carries a session id, the id is looked up in `claude agents --json` first: a listed
row in any state but a finished/stopped one is `claude stop`ped before the relaunch - a failed stop
refuses the relaunch and launches nothing - while an unlisted id (or one already finished/stopped)
proceeds without a stop.

`plangraph_session_list` reports `context_tokens` (latest assistant usage sum - input + cache read +
cache creation - from the transcript under `~/.claude/projects/`, reading only the file's last
256 KB) and `context_pct`, a fraction in 0..1 of the configured `ctx_cap_tokens`.

On Windows every `claude` invocation - the launch, `claude stop` and the `claude agents` poll -
must run the real `claude.exe` found via `which`; `.cmd`/`.bat` shims are
refused because cmd.exe would parse the prompt. Sessions start with `claude --bg`; the detached
`wt.exe` window path (exposing name + startedAt only, so the id comes from `claude agents`) still
exists in the launcher code, but no config key selects it any more.
