# R2b-2: wake daemon entrypoint + docs (lane/phase-fix-r2b, on top of R2b-1)
You are the WRITER. cwd = git worktree on branch lane/phase-fix-r2b. Edit files in place, run the checks, ONE commit. Do NOT push.
Edit only: src/plangraph/fleet_bus/wake.py (add `load_config`, `main`, `if __name__ == "__main__"` at the end; do not change WakeHook/WakeState), NEW tests/test_fleet_bus_wake_daemon.py, NEW deploy/systemd/fleet-wake.service.example, docs/FLEET-BUS.md (two new sections). Nothing else. Write your first change (a red test) within 10 minutes; anchors are below; never print whole files. <= 300 changed lines.

## Findings (verbatim)
1. MED (r2 seat B V5-1): "wake never wired (`WakeHook.run` uncalled, `client.py` passes `wake=None`, `_wake` only logs)". Brief R2-4: "Add a runnable entrypoint, `python -m plangraph.fleet_bus.wake --config <file>`. The config holds the recipient -> session map, the keeper URL, the token file and the session base. Fail closed on a bad config. Add deploy/systemd/fleet-wake.service.example (user unit, Restart=on-failure, no secrets inline, no site IP). Test: the entrypoint parses the config and drives one wake against the real keeper (World) + a stub session server. Docs: docs/FLEET-BUS.md "wake daemon"."
2. Docs gap (BUS-REGISTRY-PERSIST): "docs/FLEET-BUS.md: a short "host registry persistence" paragraph: bus_hosts table; restore forces health offline; GET hosts is joined with the inventory; no key path is stored."

## Fix
- Config = JSON file: `{"keeper_url": str, "token_file": str, "session_base": str, "state_file": str, "sessions": {"<recipient>": {"session": str, "kind": "opencode"|..., "base": optional str}}, "authorization_file": optional str}`. `load_config(path) -> (sessions: dict[str, SessionTarget], keeper_url, token, state_path, authorization)`; the token is read from token_file (strip), never logged or written.
- Fail closed: missing/unparsable file, missing key, wrong type, empty sessions, unreadable/empty token file -> `main` prints one line `fleet-wake: bad config: <reason>` to stderr (never the token, never file contents) and returns 2 BEFORE building any poll or sink (no wake, no request).
- `main(argv=None, *, stop=None) -> int`: argparse `--config` (required); builds `WakeHook(sessions, SyntheticSink(authorization=...), WakeState(state_path))` and `KeeperMessagesPoll(keeper_url, token)`, then `hook.run(poll, stop)`; `stop` defaults to a threading.Event set on SIGTERM/SIGINT; returns 0 after stop. Imports from wake_http inside `main` (wake_http imports wake: avoid a cycle).
- Unit example: `[Service] Type=simple`, `ExecStart=/usr/bin/python3 -m plangraph.fleet_bus.wake --config %h/.config/plangraph/fleet-wake.json`, `Restart=on-failure`, `RestartSec=20`; header comment "Example only"; no IP, no token, no hostname.
- Docs: `## Wake daemon` (what it does, config keys, fail-closed, the unit example, one wake per burst/at-least-once bound as wake.py's docstring states) and `## Host registry persistence` (read src/plangraph/keeper/store_bus_hosts.py and api.py `_restore_bus_hosts` ~line 553 and state ONLY what the code does for the four points; if a point is not true in code, write what is true and say so in the report).

## Invariants (each one is a test assertion; exact names in tests/test_fleet_bus_wake_daemon.py)
| # | Invariant | Input/state | Expected | Test |
|---|---|---|---|---|
| I8a | The entrypoint runs one wake end to end | valid config -> real keeper (`from keeper_util import World`, `from door_util import Doors`, pattern: tests/test_fleet_bus_wake.py `test_report_wakes_router_within_60s_on_the_long_poll` ~line 124) + stub session HTTP server recording (method, path, body); `main([...], stop=ev)` in a thread; post a report to the configured recipient | stub got exactly one POST to `/api/session/<id>/synthetic` with `{"text": "fleet-wake", "resume": true}`; main returns 0 after ev.set() | `test_daemon_wakes_the_configured_session_from_the_real_keeper` |
| I8b | Bad config fails closed | parametrize: missing file, bad JSON, no sessions, missing token file, empty token | returns 2, stderr one line starting `fleet-wake: bad config:`, the token string never in stderr, stub server got 0 requests | `test_daemon_refuses_a_bad_config` |
| I9 | No key path or site IP in tracked files | scrub_check over the new files | 0 hits | the scrub check below |

## Checks (foreground; then one commit)
- python3 -m pytest -q -p no:cacheprovider -n 4 tests/test_fleet_bus_wake.py tests/test_fleet_bus_wake_http.py tests/test_fleet_bus_wake_daemon.py tests/test_fleet_msg.py tests/test_keeper_messages.py -> 0 failed
- python3 -m pytest -q -p no:cacheprovider -n 4 (whole suite) -> 0 failed
- python3 tools/split/scrub_check.py src/plangraph/fleet_bus tests deploy docs --rules tools/split/manifest.toml --rules tools/split/scrub_ci.toml -> 0 hits
Commit trailers exactly: `Plan-Item: plangraph:RT-P1-BUS-WAKE`, `Writer: mimo/mimo-v2.6-pro <env PLANGRAPH_RUN_ID or plangraph-spawner>`. No other trailers.
Final report (< 1500 tokens): per finding FIXED <exact test name / doc section>; any docs point not true in code; the pytest tail lines. Then stop.
