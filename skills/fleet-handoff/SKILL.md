---
name: fleet-handoff
description: Use when the context guard prints HANDOFF-NOW or HARD-CAP, or before an L1/L2 session stops. Writes the full handoff file plus the fixed status card next to it, checks the card with handoff-check, then runs the guard's ready step.
---

Site keys (file `router/config` in the router data checkout; source it): `ROUTER_HOME` is the router directory; the handoff checker is `bin/handoff-check` below it. The card check writes to `ROUTER_WORKLOG`.

# Fleet handoff (every L1/L2)

1. Write the full handoff file (state, decisions, traps, next steps); commit and push as your brief says.
2. Write the card next to it as `<handoff stem>.card.md`: at most 40 lines, these 8 headings in this order, an empty section is the single line `- none`. Role is `L1` or `L2`.

```
# <role> card · <session-name> · <YYYY-MM-DDTHH:MMZ>
## Goal
## Keeper
- claimed: KEY (state, since HH:MMZ), ...
- ready next: KEY, KEY
## Done
- <MM-DDTHH:MMZ> <KEY|D-nnnn> <sha|PR#n> <evidence: CI run id / test / verdict>
## In flight
- <KEY> <run or session id> since <HH:MMZ> next check <HH:MMZ>
## Planned
1. <KEY> <why now>
## Open
- <Q-id|D-id|BLOCKED> <what> | default <x> | deadline <ts>
## Traps
## Drill-down
```

`## Done` lists everything finished since your previous card: one line each, UTC stamp, id, evidence. Write `-` for the sha only when nothing was committed.

3. Run `$ROUTER_HOME/bin/handoff-check <card> --log`. On exit 1 it prints one reason per line: fix the card and rerun until it exits 0. `--log` adds the new Done lines to `ROUTER_WORKLOG`, each once.
4. Then run the guard's `ready` step with the absolute command from the HANDOFF-NOW or HARD-CAP line: `python3 <abs path>/tools/hooks/context_guard.py ready <handoff-path>`. Never run the hook wrapper (`ctx_guard_session.py`): it has no ready step and exits 2. `ready` exits 0 only after it wrote the marker; on any other exit fix what it names and rerun. The watchdog relaunches you only from a fresh handoff plus card and alerts routing when a session at its cap has no marker.
5. Close out as your brief says (for example CARD-READY or RECYCLE-READY with the paths) and stop. Start no new work.

After a restart, run `resume-digest` first: it prints your card, keeper rows, live runs, git state and inbox on one page; read the full handoff only if that page is not enough.
