# Lane C: launcher, run tokens, MCP entry (plangraph-L2-su-launch)

STANDING RULES: `docs/plans/orch-setup/briefs/COMMON.md` (binding, read first). Design sections: 2, 3, 6 (Launcher, Run tokens,
MCP entry).
Items, in order:
- `plangraph:SU-LAUNCH`, `routing:SU-FLEET-LAUNCH-WRAP`, `plangraph:SU-RUN-TOKENS` (HIGH);
- `plangraph:SU-MCP-RS` (HIGH, can run in parallel with SU-LAUNCH);
- `plangraph:SU-MCP` (HIGH).

Starts once SU-CORE is merged; SU-MCP-RS also needs SU-ENV.

## SU-MCP-RS spec (HIGH)
- `plangraph mcp`: a stdio JSON-RPC MCP server.
  - The same tool names, input schemas, outputs and error codes as `src/plangraph/keeper/mcp.py` (tool table at `mcp.py:40-119`).
  - Each call is forwarded to the keeper HTTP API: `PG_URL`, bearer from `PG_TOKEN_FILE` (read, never logged), mTLS files if
    set.
  - Keep the cleartext warning for a non-loopback http URL.
- Contract test: start a test keeper and run every tool through both servers; outputs equal (normalised timestamps).
- Allowed extra deps: a blocking HTTP client with rustls (ureq or minreq). No async runtime.

## READ
- routing `bin/fleet-launch` (WSL clone `~/fleet/routing`, `git pull` first) and its `bin/ctx-watchdog` (record consumer).
- workstation `C:\fleet\launch\start-*.ps1`: the current launch shape (`claude --model .. --effort .. -n <name>
  --remote-control <name> <prompt>`). Read only.
- plangraph `src/plangraph/keeper/mcp.py` (tools `mint_run_token`, `whoami`, `heartbeat`) and the keeper code behind
  `mint_run_token` (`git grep -n mint_run_token origin/main`): what a run token is bound to.
- AutoOS `lib/windows/AutoOS.Install.psm1` `Register-AutoOSMcpServer` (about line 1173): the idempotent `claude mcp` pattern.

## WRITE SCOPE
- plangraph: `rust/plangraph-cli/src/launch/**`, `rust/plangraph-cli/src/mcp/**`, `rust/plangraph-cli/src/setup/mcp.rs`, their
  tests, the `[mcp]` section of
  `setup/manifest.toml`, `docs/SETUP.md` (launch + MCP sections).
- routing: `bin/fleet-launch` plus its test.

## SU-LAUNCH spec
- Arguments: `plangraph launch <name> --cwd D --model M --effort E --prompt-file F [--parent P] [--bg] [--mcp C] [--handoff H]
  [--respawn] [--dry-run]`.
- Generate a UUID v4 and write the record `<FLEET_LAUNCH_DIR>/<name>.json` BEFORE starting. Fields: `name, cwd, model, effort,
  mcp, prompt_file, handoff, parent, children, ts, session_id` plus `env` (windows|wsl|linux). `--respawn` semantics equal
  fleet-launch.
- Start `claude --session-id <uuid> -n <name> --remote-control <name> --model M --effort E [--bg ...] <prompt>` with env
  `FLEET_SESSION_NAME=<name>` and `PG_TOKEN_FILE` (see SU-RUN-TOKENS).
- First test whether `--bg` accepts `--session-id`. If it does not, fall back to parsing the UUID from stdout (as today) and
  record the fallback.
- `--dry-run` prints the command with the prompt redacted to its length and writes nothing.
- Produce `docs/SETUP.md` guidance and a generator so a `start-<name>.ps1` is one line calling `plangraph launch`. Do NOT edit
  `C:\fleet\launch` yourself; workst-L1-autoos switches the files.

## SU-FLEET-LAUNCH-WRAP spec
`bin/fleet-launch` keeps its CLI and exit codes and delegates to `plangraph launch --bg` when the binary is found. Otherwise
it keeps the old path with a warning line. A dry-run output test covers both paths. ctx-watchdog is unchanged.

## SU-RUN-TOKENS spec (HIGH)
- With `--parent`, the launcher calls keeper `mint_run_token` using the parent's own `PG_TOKEN_FILE`. It writes the result to
  `~/.local/state/plangraph/tokens/<name>.token` (0600; on Windows owner-only ACL) and sets the child's `PG_TOKEN_FILE` to it.
- The token is never printed or logged; length + sha256 prefix only. It expires with the lease and is revoked on
  `plangraph launch --end <name>`, called by the parent or the Stop path. Document the end path.
- An L1/planner (no `--parent`) uses `~/.config/plangraph/tokens/plangraph-<name>.token` if present; otherwise it gets no
  token and has read access via the snapshot only.

## SU-MCP spec (HIGH)
- `setup apply` registers `plangraph` at user scope through `claude mcp add-json --scope user` with command
  `<abs path to plangraph> mcp`. No token in the entry; env comes from the env file and the launcher.
- Idempotent via `claude mcp get plangraph`: an identical entry is skipped, a different one is reported as drift. `--force`
  replaces only our entry.
- `setup check` runs `whoami` when `PG_TOKEN_FILE` is set.
