# L2 brief · build-L2-routermove · from build-L1-0039 · 2026-10-31
Load skill unattended-orchestration (rules R-orch-13/14, R-coord-09/12). Output contract ~/.claude/output-contract.md.

## Goal / done means
Two keeper lanes (owner build-L1, delegated to you; read `show <key>` first, re-confirm the premise against D-3431/D-3434/D-3435):
1. plangraph:LE-PANE-MOVE-EXIT-CONFIRM: router/repoint/move-router-pane.sh answers the /exit 'background tasks' confirm dialog (or stops bg tasks first), detects a startup dialog before the resume prompt (resend once or FAIL with reason), reports 'moved' only after the router turn started. Fake-herdr tests.
2. plangraph:LE-FLEET-MSG-INVENTORY: after a scripted router (re)start ListAgents shows routing-<seq>; fleet_msg_send to routing works (FLEET_MSG_INVENTORY unset today, src/plangraph/fleet_bus/client.py); router/bin/oom-protect-router matches a router started as bare 'claude' (fake pgrep test). Router .claude/settings.local.json is operator-owned: do not create it.
Done per item: merged, CI green, 1 non-anthropic seat with a VERDICT line PASS (D-3420), MERGE-READY to build-L1 with sha.

## Files
Own: plangraph-data router/repoint/move-router-pane.sh, router/bin/oom-protect-router, router/repoint/staged/oom-protect-router.service.d/*, their tests under router/tests; plangraph src/plangraph/fleet_bus/client.py (+ its tests) only if the inventory fix lives there.
Never: router/bin/ctx-watchdog (lane LE-WATCHDOG-CLEAR-VERIFY open), keeper engine, AUTH_DOOR_PATHS, any live unit/timer/router pane, ~/.omniroute.
Worktrees: /home/s/code/plangraph-data-worktrees/<item-slug>, /home/s/code/plangraph-worktrees/<item-slug>, cut from origin/main.

## Rules
- Writers only via the PLANGRAPH spawner (D-2918): free OpenCode/OpenRouter legs first, then qwen / mimo-pro; no Claude writers (D-3409). <4 runs + MemAvailable >= 2000 MB checked in the same command as each spawn.
- Seats non-anthropic and not any writer family of the PR (D-3084, D-2511); NO-TOOLS seat header, diff + item only, no rulings (D-302). All findings to one fix writer per round, round cap 3 (D-2739).
- Hand-edit cap 30 lines with Hand-Edit trailer; trailers in ONE last paragraph; writers never write Test-Removal/Gate-Edit.
- Writers never read /proc, env, keeper config/DB (D-3428). No live steps: anything touching the running router goes to build-L1 as GO-REQ.
- Never merge or push to main; no crons; card <= 40 lines at status/handoff.card.md in your worktree; RECYCLE-READY before the cap.
- Disputes/decisions: BLOCKED one line to build-L1.

## Report
MERGE-READY <item> <sha> <PR> <CI> <seat run+verdict> | BLOCKED ... | FAIL ...; final message <= 10 lines. Report, then stop.
