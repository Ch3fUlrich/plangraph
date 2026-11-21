# Fail-closed PreToolUse guards

`guard-hook GUARD TIMEOUT_MS PATH` forwards the guard's healthy result,
including structured denials and exit-2 denials. It runs Python guards through
the host Python interpreter and runs native guards as executables. Infrastructure
failures exit 2. The runner emits `GUARD-OUTAGE` once per guard outage and writes
the incident to `$FLEET_STATE_DIR/guard-outages/GUARD.alert`. A subsequent
healthy policy result clears that marker. The collector of host logs can also
read the first notice from the hook's stderr.

Use Claude Code 2.1.295 or newer and set `onFailure` to `block` on the
effective PreToolUse entries. This covers a runner that Claude Code itself
cannot start or run within its timeout. Allow the runner to time out first so
it can record its incident. A host already using `context_guard.py` can pass
that file. A host with per-session thresholds uses `ctx_guard_session.py`.

For a scoped fleet rollout, use the tested hook entries from the effective
settings fixture for the host version. Replace the managed Bash, context,
and turn-budget entries. Preserve other hook registrations when layering the
settings. Verify the effective entries on each deployed host.

- [Claude Code 2.1.323 settings fixture](../rust/pg-hook/tests/fixtures/claude-hook-settings/2.1.323.json)
- [Claude Code 2.1.329 settings fixture](../rust/pg-hook/tests/fixtures/claude-hook-settings/2.1.329.json)
