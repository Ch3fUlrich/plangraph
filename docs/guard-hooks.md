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

Example entries for the fleet settings, after the scoped deployment GO

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          {
            "type": "command",
            "command": "\"$HOME/.local/bin/guard-hook\" bash 5000 \"$HOME/.local/share/plangraph/hooks/bash_guard.py\"",
            "timeout": 10,
            "onFailure": "block"
          }
        ]
      },
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "\"$HOME/.local/bin/guard-hook\" context 5000 \"$HOME/.local/share/plangraph/hooks/ctx_guard_session.py\"",
            "timeout": 10,
            "onFailure": "block"
          }
        ]
      }
    ]
  }
}
```

Replace the existing Bash and context PreToolUse entries with those commands.
Keep the existing registration of other hook events. Verify the effective
PreToolUse entries for every deployed Claude Code version.
