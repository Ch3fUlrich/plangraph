# Supervised HTTP MCP Servers (WS-MCP-AUTORECONNECT)

## Why Supervised HTTP

Workstation MCP servers run by default over `stdio`. In interactive Claude Code sessions, when a `stdio` server crashes or exits, Claude Code does not restart it automatically; human intervention (`/mcp` Reconnect) is required.

In contrast, HTTP/streamable-HTTP MCP endpoints are retried automatically by Claude Code (5 attempts with exponential backoff starting from 1 s, totaling ~31 s). Running MCP servers as supervised local HTTP endpoints makes workstation MCP connections resilient to server restarts and transient crashes.

### Incident Root Causes (2026-10-22 / 2026-10-23)

| Date | Incident Trigger | Impact on stdio | Resolution in Supervised HTTP |
|---|---|---|---|
| 2026-10-22 | Fallback agent ran `taskkill /F /IM python.exe` | All Python MCP servers killed simultaneously | Supervisor restarts exited child processes; pwsh loop restarts supervisor |
| 2026-10-23 | Keeper MCP stdout crash on Windows default `cp1252` encoding | Process aborted on non-ASCII output | `PYTHONUTF8=1` enforced in child environment by supervisor |

## Design

Each shared server runs behind `mcp-proxy` (pinned version `0.13.0`), exposing a streamable HTTP JSON-RPC endpoint at `http://127.0.0.1:<port>/mcp`:

1. **Stdlib-only Supervisor (`plangraph.mcp_supervise`)**: Launches and monitors child processes without a shell. Enforces bind address `127.0.0.1` (never `0.0.0.0`) and valid port ranges (1024–65535).
2. **Backoff and Recovery**: On unexpected exit, restarts the server with exponential backoff (1s, 2s, 4s, 8s, 16s, capped at 30s). When a server stays healthy for >= 300 s, backoff resets to 1s.
3. **Environment and Log Hygiene**: `PYTHONUTF8=1` is injected into every child environment. Log files record start, exit, restart, PID, and return codes, but never log environment variable values or secrets.
4. **Scope Isolation (Host vs. Project)**: Serena holds project-specific state (`activate_project`). Sharing a single Serena instance across different repositories causes session conflict. The config supports `scope: "project"` with dedicated ports and `project_dir` per workspace, while global utilities use `scope: "host"`.

## Usage

### 1. Running the Supervisor

Run the supervisor daemon directly:

```powershell
python -m plangraph.mcp_supervise run --config examples/mcp-supervise.workstation.json
```

Or via the persistent Windows PowerShell launcher:

```powershell
.\tools\mcp-supervise.ps1 -Config examples\mcp-supervise.workstation.json
```

### 2. Applying Claude Code Configuration

Render the `mcpServers` configuration:

```powershell
python -m plangraph.mcp_supervise render --config examples/mcp-supervise.workstation.json
```

Output:

```json
{
  "plangraph-keeper": {
    "type": "http",
    "url": "http://127.0.0.1:8100/mcp"
  },
  "autoos-agent": {
    "type": "http",
    "url": "http://127.0.0.1:8101/mcp"
  },
  "serena-plangraph": {
    "type": "http",
    "url": "http://127.0.0.1:8102/mcp"
  }
}
```

Copy the output into `%USERPROFILE%\.claude.json` under `"mcpServers"`. This configuration is applied manually by an operator.

### 3. Kill Test Verification

Verify automatic recovery using the `check` command:

```powershell
python -m plangraph.mcp_supervise check --config examples/mcp-supervise.workstation.json --name plangraph-keeper
```

This finds the child PID, kills its process tree, and polls the streamable-HTTP `initialize` probe. If healthy within 60 seconds, it prints elapsed time and exits 0.

## Process Limit and PowerShell Launcher

If an agent or human runs `taskkill /F /IM python.exe`, a Python supervisor daemon would itself be killed. To prevent host-wide name-kills from permanently disabling supervision, `tools/mcp-supervise.ps1` runs as a PowerShell (`pwsh.exe`) loop. When all `python.exe` processes terminate, `pwsh` survives, sleeps 2 seconds, and relaunches the supervisor daemon. The PowerShell launcher is intended to run as a hidden Windows scheduled task (registered as a separate live operational step).
