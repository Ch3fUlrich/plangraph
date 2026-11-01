# Background Session Reaper (`SESSION-AUTO-CLOSE`, D-2442)

Reaps idle and finished Claude Code background sessions residing in `~/.claude/jobs/<short-id>/state.json`.

## Reaping Policy

Decisions are evaluated per session (`state.json`), in this order. Every guard that keeps a session runs before the timing rules, and an input that cannot be trusted keeps the session (fail closed).
1. **Name**: a missing, empty or non-string `name` is unknown and kept (reason `unknown_name`).
2. **Allowlist**: names matching these globs are always kept (reason `allowlisted`): `*-L1-*`, `routing*`, `*judge*`, `*planner*`, `plangraph-oversight*`. Matching is case-insensitive on every platform. A `--config` allowlist **adds** patterns; it never replaces these defaults.
3. **TTL marker**: if `ttl.json` exists next to `state.json`, `allow_stop: false` keeps the session. A marker that is unreadable, not an object, has `allow_stop` other than `true`/`false`/`0`/`1`, or has a `ttl_min` that is non-numeric or 0 or below keeps the session (reason `ttl_invalid`).
4. **State guard**: only `state == "done"` can be stopped. `working` (even with tempo `idle`), `blocked` and `stopped` are always kept.
5. **Timestamp**: needs a real `updatedAt` with an explicit UTC offset. Missing, naive, unparseable or future timestamps keep the session (`createdAt` is never used).
6. **Thresholds**, never shorter than 1 minute (a non-numeric `done_grace_min` or `idle_min` in the config keeps the session, reason `config_invalid`):
   - `ttl_min` from the marker, when set, applies to the session whether or not it has a result.
   - otherwise `done` with an output result: `done_grace_min` (default 2 min);
   - otherwise `done` without a result: `idle_min` (default 30 min).
7. **Re-check before each stop**: `state.json` is read again immediately before `claude stop`. If it is no longer `done`, its `updatedAt` moved, or the policy no longer says stop, the session is skipped (reason `changed_since_scan`). A short window between that read and the stop remains.
8. **Stop primitive**: `claude stop <id>` only, which keeps the conversation resumable. Processes are never killed by PID.

## Session TTL Marker

Launchers (`fleet-launch`, `spawner`) set a per-session marker:
```bash
python -m plangraph.session_reap.mark --ttl-min 45
# Or to prevent automatic reaping:
python -m plangraph.session_reap.mark --ttl-min 60 --no-stop
```
This writes `ttl.json` (`{"ttl_min": N, "allow_stop": bool}`) into the session's own job dir: the nearest directory at or above `CLAUDE_JOB_DIR` that contains `state.json`. It refuses if the variable is unset or no such directory exists, and rejects `--ttl-min` below 1. An explicit `--dir` must itself contain `state.json`; it is never created.

## Running Once

Dry run (default, report only):
```bash
python -m plangraph.session_reap
python -m plangraph.session_reap --json
```
Options: `--jobs-dir D` (default `~/.claude/jobs`), `--config F`, `--apply`, `--json`. The report lists every session; state files that could not be read are counted and shown (table footer, or `"unreadable"` in the JSON object `{"sessions": [...], "unreadable": N}`) and are never stopped.

Apply stops (executes `claude stop <id>` for eligible sessions):
```bash
python -m plangraph.session_reap --apply
```
Every stop attempt is appended to `~/.local/state/plangraph/session-reap.jsonl` with `ts`, `host`, `short_id`, `name`, `age_min`, `state`, `reason`, `action`, `rc`. A failed stop is logged with `rc` non-zero (a timeout or a runner exception is `rc` 1) and `reason` gains a suffix `;stop_failed:timeout`, `;stop_failed:runner_error` or `;stop_failed:nonzero_rc`.

### Config file (`--config`)

JSON object, all keys optional, unknown keys rejected:
```json
{"done_grace_min": 2, "idle_min": 30, "allowlist": ["my-long-job*"]}
```
`done_grace_min` and `idle_min` are positive numbers; `allowlist` is a list of non-empty globs added to the defaults. A missing, unreadable or invalid config aborts the run with exit code 2 and stops nothing; it never falls back to defaults.
## Scheduling (Example Only - Do Not Install)

Scheduling periodic execution is an operational step not installed by this package.

### Linux: systemd user service & timer

`~/.config/systemd/user/session-reap.service`:
```ini
[Unit]
Description=Claude Code Background Session Reaper

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 -m plangraph.session_reap --apply
```

`~/.config/systemd/user/session-reap.timer`:
```ini
[Unit]
Description=Run Claude Code session reaper every 5 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=5min

[Install]
WantedBy=timers.target
```

### Windows: Scheduled Task (PowerShell)

```powershell
$Action = New-ScheduledTaskAction -Execute "python.exe" -Argument "-m plangraph.session_reap --apply"
$Trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 5)
$Settings = New-ScheduledTaskSettingsSet -Hidden -StartWhenAvailable
Register-ScheduledTask -TaskName "ClaudeSessionReaper" -Action $Action -Trigger $Trigger -Settings $Settings
```
