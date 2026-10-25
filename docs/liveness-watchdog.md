# Liveness Watchdog

`tools/liveness_watchdog.py` is a generic external watchdog designed to run outside agent sessions. It monitors the heartbeat (stamp) and health of configured targets and takes recovery actions (alerting and relaunching) if a session stalls or dies. Because it runs independently (for example, as a systemd user timer), it can detect and recover sessions whose internal schedulers or event loops have stopped.

---

## What the Watchdog Does

Each execution of `tools/liveness_watchdog.py` evaluates all targets configured in a configuration file:

1. **Stamp Age Calculation**:
   - For each target, the watchdog reads the stamp file specified in `stamp`.
   - Age is computed as `now - timestamp` (in seconds).
   - If the stamp file is missing, unreadable, or invalid/corrupt JSON, its age is considered infinite (`inf`, "stale forever").
   - If the stamp file starts with `{`, it is parsed as JSON and must contain a numeric `ts` key (Unix epoch in seconds).
   - If the file is not JSON, the filesystem modification time (`st_mtime`) is used as the timestamp.

2. **Alert Once per Episode**:
   - When `age > alert_after_min * 60`, the target is stale.
   - If the target has not yet alerted in the current stale episode, the watchdog executes `alert_cmd` with the alert message appended as the final argument, and appends a record to `inbox` (if configured).
   - The episode state records `alerted = true` so the alert fires **at most once per episode**.

3. **Relaunch Once per Episode**:
   - A relaunch is triggered if either:
     - `relaunch_after_min` is configured and `age > relaunch_after_min * 60`, OR
     - `alive_cmd` is configured and exits with a non-zero status (or fails to run/times out).
   - If `relaunch_cmd` is configured and `relaunch_after_min` is not null, the watchdog executes `relaunch_cmd` with the relaunch message appended as the final argument, recording `relaunched = true` (**at most once per episode**).
   - If `relaunch_after_min` is null or `relaunch_cmd` is not configured, the target is considered **alert-only**; the watchdog logs that relaunch was due but skipped (`noted = true`), without attempting execution.

4. **Health Check Probe (`alive_cmd`)**:
   - An optional probe command list executed during `--apply` passes.
   - Exit code 0 indicates healthy/alive. Any non-zero exit code, command timeout, or startup failure indicates not alive.
   - Reporting not alive immediately triggers relaunch (if configured), without waiting for `relaunch_after_min`.
   - `alive_cmd` is only executed during `--apply` passes; dry runs judge staleness from stamp age alone.

5. **Episode Reset**:
   - A stale episode ends when the target becomes healthy again: the stamp is fresh (`age <= alert_after_min * 60`) AND `alive_cmd` (if configured) exits 0.
   - When healthy again, the watchdog resets episode flags (`alerted = false`, `relaunched = false`, `noted = false`) and logs an episode reset. Subsequent staleness will trigger a new episode with fresh alerts and relaunches.

6. **Dry Run by Default vs. `--apply`**:
   - By default (without `--apply`), the watchdog runs in **dry-run mode**. It evaluates stamp age, logs what it would alert or relaunch, executes no commands (including `alive_cmd`), and makes no changes to disk (no state files, directories, or inbox entries written).
   - Passing `--apply` enables active enforcement: commands are executed, episode state files are updated atomically, and inbox lines are appended.

7. **Command Execution Safety & Exit Codes**:
   - All configured commands (`alive_cmd`, `alert_cmd`, `relaunch_cmd`) are argv lists executed verbatim (no shell parsing).
   - Each command has a 120-second hard timeout.
   - Command errors, timeouts, or non-zero exits are logged (capturing at most the last 200 characters of flattened output) and never crash the watchdog pass.
   - Exit status is `0` for every evaluated pass (even when target commands fail). Exit status is `2` only when the configuration file cannot be read, fails to parse, or lacks required top-level keys (`state_dir`, `targets`).

---

## Configuration Format

Configuration files can be written in **TOML** (standard) or **JSON**. The top-level structure and all parsed keys are documented below:

### Top-Level Keys

| Key | Type | Default | Description |
|---|---|---|---|
| `state_dir` | string | *(required)* | Path to the directory where per-target episode state is stored (`liveness-<name>.json`). Must be a non-empty string. |
| `inbox` | string | `null` (omitted) | Optional path to an alert inbox file. When set, each alert appends `<iso-utc-timestamp> ALERT <name>: <message>`. |
| `targets` | list of tables | *(required)* | List of target tables/objects to monitor. |

### Target Keys (`[[targets]]`)

| Key | Type | Default | Description |
|---|---|---|---|
| `name` | string | *(required)* | Unique identifier for the target. Must be non-empty, cannot be `.` or `..`, and must not contain `/` or path separators. Used in state file naming: `<state_dir>/liveness-<name>.json`. |
| `stamp` | string | *(required)* | Path to the stamp file. May be a JSON stamp written by `stamp` subcommand (must contain numeric `ts`) or any file whose `mtime` is tracked. |
| `alert_after_min` | number (float/int >= 0) | `45.0` | Inactivity threshold in minutes before an alert is emitted. |
| `relaunch_after_min` | number (float/int >= 0) or `null` | `75.0` | Inactivity threshold in minutes before a relaunch is triggered. Setting to `null` makes the target alert-only. |
| `alive_cmd` | list of strings | `null` (omitted) | Optional health probe argv list. Exit 0 = healthy; non-zero = trigger relaunch. Executed only when `--apply` is passed. |
| `alert_cmd` | list of strings | *(required)* | Command argv list to run on alert. The alert message string is appended as the final argument. |
| `relaunch_cmd` | list of strings | `null` (omitted) | Command argv list to run on relaunch. The relaunch message string is appended as the final argument. If omitted or null, the target is alert-only. |

---

## Example Configuration

Below is a generic TOML configuration using placeholder commands and paths under `%h` (or placeholder paths). No real host names, tokens, or credentials are used:

```toml
# %h/.config/plangraph/liveness/router-prod.toml
state_dir = "%h/.local/state/plangraph/liveness"
inbox = "%h/.local/state/plangraph/liveness/inbox.log"

[[targets]]
name = "router-worker"
stamp = "%h/.local/state/plangraph/router/heartbeat.stamp"
alert_after_min = 30
relaunch_after_min = 60
alive_cmd = ["/usr/bin/pgrep", "-u", "<user>", "-f", "<service-pattern>"]
alert_cmd = ["/usr/bin/logger", "-t", "liveness-watchdog", "--priority", "user.warn"]
relaunch_cmd = ["/usr/bin/systemctl", "--user", "restart", "<worker-service-name>.service"]

[[targets]]
name = "scheduler-task"
stamp = "%h/.local/state/plangraph/scheduler/beat.json"
alert_after_min = 45
relaunch_after_min = 90
alert_cmd = ["/bin/sh", "-c", "echo \"$@\" >> %h/.local/state/plangraph/alerts.txt", "--"]
relaunch_cmd = ["/bin/echo", "relaunching scheduler"]

[[targets]]
name = "metrics-collector"
stamp = "%h/.local/state/plangraph/metrics/pulse.stamp"
alert_after_min = 15
relaunch_after_min = null  # Alert-only target; no relaunch attempted
alert_cmd = ["/usr/bin/logger", "-t", "liveness-watchdog", "--priority", "user.err"]
```

---

## How a Session Writes Its Stamp

A running session or heartbeat loop writes its stamp atomically on every beat using the `stamp` subcommand:

```bash
python3 tools/liveness_watchdog.py stamp <path-to-stamp>
```

This writes a JSON payload via a temporary file in the same directory followed by an atomic rename (`os.replace`):

```json
{
  "ts": 1729832400.123456,
  "iso": "2026-10-25T05:00:00Z",
  "pid": 12345
}
```

- `ts`: Current Unix timestamp (float).
- `iso`: UTC ISO-8601 formatted timestamp string.
- `pid`: Process ID of the process writing the stamp.

The watchdog parses `ts` to determine the stamp's age. If a target instead uses a non-JSON file (or plain file touched by an external tool), the watchdog falls back to the file's modification time (`st_mtime`).

---

## Installation Steps (systemd User Units)

The watchdog runs as a systemd user service triggered by a systemd user timer.

### 1. Copy Unit Templates

Copy both templates to the user systemd unit directory:

```bash
mkdir -p ~/.config/systemd/user
cp deploy/liveness/liveness-watchdog@.service ~/.config/systemd/user/
cp deploy/liveness/liveness-watchdog@.timer ~/.config/systemd/user/
```

### 2. Write the Configuration

Create the target configuration file matching the desired instance name `<name>`:

```bash
mkdir -p ~/.config/plangraph/liveness
# Edit ~/.config/plangraph/liveness/<name>.toml with appropriate paths and commands
```

### 3. Reload systemd Manager Configuration

```bash
systemctl --user daemon-reload
```

### 4. Enable and Start the Timer

```bash
systemctl --user enable --now liveness-watchdog@<name>.timer
```

### 5. Enable User Lingering (Persistent User Session)

To allow the user timer and service to run without an active interactive login session:

```bash
loginctl enable-linger "$USER"
```

### 6. Verification and Inspection

```bash
# Check timer schedule and next trigger time:
systemctl --user list-timers 'liveness-watchdog@*'

# Run one pass manually now:
systemctl --user start liveness-watchdog@<name>.service

# Inspect recent watchdog logs in journald:
journalctl --user -u liveness-watchdog@<name>.service -n 20
```

### 7. Stopping and Disabling

```bash
systemctl --user disable --now liveness-watchdog@<name>.timer
```

---

## Operator / Router GO Requirement

> [!IMPORTANT]
> **Enabling a live instance requires explicit operator/router GO approval.**
> Do not enable or start `liveness-watchdog@<name>.timer` or `liveness-watchdog@<name>.service` against production targets without the required authorization and coordinated confirmation from the router operator.
