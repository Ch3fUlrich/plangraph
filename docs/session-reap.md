# Background Session Reaper (`SESSION-AUTO-CLOSE`, D-2442)

Reaps idle and finished Claude Code background sessions residing in `~/.claude/jobs/<short-id>/state.json`.

## Reaping Policy

Decisions are evaluated per session (`state.json`):
1. **Allowlist**: Session names matching glob patterns (`*-L1-*`, `routing*`, `*judge*`, `*planner*`, `plangraph-oversight*`) are always kept (reason: `allowlisted`).
2. **TTL Marker**: If `ttl.json` exists next to `state.json`, `allow_stop: false` keeps the session. `ttl_min` overrides default `idle_min`.
3. **State Guard**: Only sessions in `state == "done"` can be stopped. Sessions in `working` (even with tempo `idle`, waiting on background tasks), `blocked` (waiting on human), or `stopped` are always kept.
4. **Done Grace / Idle Thresholds**:
   - `done` with output result: stopped after `done_grace_min` (default 2 min).
   - `done` without output result: stopped after `idle_min` (default 30 min, or `ttl_min`).
5. **Stop Primitive**: Sessions are stopped exclusively via `claude stop <id>`, preserving resumable conversation history. Processes are never killed by PID.

## Session TTL Marker

Launchers (`fleet-launch`, `spawner`) can set per-session TTL by calling:
```bash
python -m plangraph.session_reap.mark --ttl-min 45
# Or to prevent automatic reaping:
python -m plangraph.session_reap.mark --ttl-min 60 --no-stop
```
Writes `ttl.json` into the session directory (derived from `CLAUDE_JOB_DIR` parent).

## Running Once

Dry run (default, report only):
```bash
python -m plangraph.session_reap
python -m plangraph.session_reap --json
```

Apply stops (executes `claude stop <id>` for eligible sessions):
```bash
python -m plangraph.session_reap --apply
```
Every applied stop is appended to `~/.local/state/plangraph/session-reap.jsonl` with audit metadata (`ts`, `host`, `short_id`, `name`, `age_min`, `state`, `reason`, `action`, `rc`).

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
