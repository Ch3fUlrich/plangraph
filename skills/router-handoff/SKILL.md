---
name: router-handoff
description: Use before the router's context is cleared or the session is relaunched, or when the operator asks for a handoff. It brings the state card, HANDOFF.md, CHANGELOG.md and docs/LESSONS.md up to date, then commits and pushes, so the next router generation acts from its first turn.
---

Source `router/config` in the router data checkout. Use `ROUTER_HOME` for
router-relative paths, `ROUTER_CARD` for the state card and
`ROUTER_SESSION_NAME` for its session label. Read the data checkout's
configured upstream before committing or pushing a handoff.

# Router handoff (restart design, D-040, D-080)

1. **Rewrite `ROUTER_CARD`** in the fixed fleet card format (40 lines or fewer; the SessionStart hook prints it), exactly:
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
   - Header: role `router`, session name `ROUTER_SESSION_NAME`. All 8 headings, in this order; an empty section is the single line `- none`.
   - `## Done` = everything finished since the previous card, one line each: UTC stamp, id, sha or PR#n (`-` only when nothing was committed), evidence.
   - Goal and channel go under `## Goal`; next 1..n with shas and lanes under `## Planned`; open items and open operator steps under `## Open` (an operator step: `BLOCKED OS-nnn <what> | default <x> | deadline <ts>`).
   - Router extras go under `## Traps` (only measured ones: state of fleet, gateway and hosts, heartbeat and handoff lines such as the CronList reminder) or `## Drill-down` (pointers).
2. **HANDOFF.md**: §3 session ids, §4 what runs and waits, §6 crons (exact cron prompts), §7 new traps.
3. **CHANGELOG.md**: today's section, one line per landed or decided item, with D-ids.
4. **docs/LESSONS.md**: each new measured lesson with its D-id or incident. Mechanical ones are candidates for code (D-103): also send them to the build L1.
5. Update the progress record named in the handoff with plan percentages and workstreams. No plan page or plan artifact (D-1277): the plan lives only in the plan graph (plangraph keeper); never republish the retired plan artifacts.
6. `git add -A && git commit` (with the attribution trailer) and push to the verified upstream of the current branch. Check `git status -sb` shows no `ahead`.
7. `cd "$ROUTER_HOME" && bin/handoff-check "$ROUTER_CARD" --log`. It must exit 0; otherwise it prints one reason per line: fix the card, redo step 6, run it again. `--log` adds the card's new Done lines to `FLEET_WORKLOG`, which `bin/work-summary` reads.
8. **Last tool call of the turn:** run the installed context guard from the restart handoff with `ready HANDOFF.md`. Then end the turn with one line and start NO new work: bin/ctx-watchdog stops this session within about 5 minutes and starts a NEW one (`claude stop`, then `bin/fleet-launch router ... --respawn` as `<router role>-NNNN`, restart prompt; never /clear + /rename, D-3610). If you must act before that, redo steps 1, 6, 7 and 8 afterwards.

The successor (a new session) gets the card via the hook. It runs `CronList` and reports in 5 lines or fewer how well the card worked (D-096).

After a restart, run `resume-digest` first: it prints your card, keeper rows, live runs, git state and inbox on one page; read HANDOFF.md only if that page is not enough.
