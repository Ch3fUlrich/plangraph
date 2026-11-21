---
name: router-heartbeat
description: Use on every router heartbeat, and whenever an orchestrator's message or report arrives in the router session. It runs the status digest, verifies claims read-only, routes execution to the owning L1, and logs results, spending as few tokens as possible.
---

Source `router/config` in the router data checkout. Resolve `bin/`,
`DECISIONS.md`, `CHANGELOG.md`, `QUESTIONS.md` and `docs/` under `ROUTER_HOME`.
Use `ROUTER_SESSION_NAME` for the session label and the live session's
CronList for its heartbeat schedule.

> **Target architecture = the rethink design (operator D-1834):** read the design and operator answer rounds cited by the D-1834 keeper decision. Goal: as flat as possible, least Claude: no persistent L0/L1/L2 LLM sessions; keeper (state) + Temporal (execution) + Rust worker + code accepts; Claude only as attended planner and on-demand judge; rules decide routing, Jev in shadow (RT-P5-JEV); one card per worker session on free or subscription legs. The L0-L3 layer text below is transitional, never the plan. Explain the plan only from the design file.

# Router heartbeat (`ROUTER_SESSION_NAME`, L0)

A quiet beat is ONE line (thin router, D-228). Never read whole inbox files.

1. `bin/routerctl status`. Act only on its lines.
   - `!` merged idle worktrees: run `bin/routerctl cleanup` (dry run) and send the list to the L1 that owns them. Never remove them yourself.
   - `MemAvailable` below 6 GB: no new local dispatch (D-081). Say so only if a dispatch is pending.
   - `ready ...` / `done ...` lines: nothing, unless one claims something the operator will hear about.
1b. **Stale-ready check (D-411):** use the routerctl `stale-ready` line and the live keeper API (`/v1/show?key=`); never a local keeper.db copy (D-3524). Items in state `open`, no unfinished `depends_on`, `updated_at` older than 6 h: one line per owner L1, "start now or say why". Operator-order items first.
1c. **Blocking states reach the operator (D-942).** A routerctl line "BLOCKED (waits for input...)" or a message `BLOCKED <item> | <what> | options, *default | deadline` is never left in that session's chat. In the same beat: while the operator is active, ask with AskUserQuestion (default first), listing every blocked item in ONE call (multiSelect, D-1001); otherwise send ONE PushNotification (under 200 chars, leads with what to decide) and put a QUESTIONS.md row with Options, `*default`, Deadline. Re-notify only after 3 h unanswered. Do not push routine progress.
1d. **Keep every project working (D-953).** For each L1 shown idle or silent whose project has ready items (state `open`, no unfinished `depends_on`, owner that L1): send ONE line `NEXT <item-key>[, <item-key>...] | <batch title> - start it as one batch or send BLOCKED` naming every related ready item of one batch (rule 3.56 (1), D-6357), never one item per writer. Work stops only at a real blocking state (then 1c). Do not nudge busy sessions.
1e. **Depth rule check (D-887, DECISIONS 2.1).** Count per L1 the keeper items in state `claimed` against live L2 sessions (`claude agents --json`, names `*-L2-*`). An L1 with 2 or more claimed items and no live L2 gets ONE line `DEPTH: <n> claimed, no L2: put phase <x> under a Sonnet L2 (L2-LAUNCH) or say why` (single-task fixes may stay flat). Log repeated misses in docs/LESSONS.md.
1f. **Address check (D-1015).** `ListAgents`: every L1 expected in projects.yaml appears once and not `offline`; two rows with one name mean the old id is dead, use the live `[ref]`. A restarted session gets the recovery message (handoff, skill read, `pg ready`, resend unconfirmed) and replies `<NAME>-ALIVE <id>`. Never send to a stored session id.
1g. **Cold sessions (D-3642, rule 4.02).** A routerctl `! cold (...)` line lists idle background sessions whose prompt cache expired (idle 60 min or more, ctx 100k or more): every wake re-writes the whole context. Never wake one for a bare status ping or a 1d NEXT line alone (also not for a D-418 `silent` line): fold everything pending into ONE message and send it only when work must reach that session. A cold session whose work is done, or that has a fresh card, is relaunched from the card (router-launch: `bin/fleet-launch <role> <cwd> <model> <effort> <restart prompt> --respawn`), not resumed. Check the heartbeat schedule with CronList.
1h. **Legs and route (OPERATOR D-5645, D-5658).** Every beat: the leg-alert command from the heartbeat handoff (until `routerctl status` prints these lines under ROUTER-GO-FACTS-AND-LEG-ALERTS) and one spawner route call (`plangraph_agent_route`, role review, writer family of the lanes in flight). Act on every ALERT line in the same beat: name a serving family to the owner L1, stop re-spawns on a leg that does not serve, report a rate drop to the operator the same hour. The same failure class twice (seat without a leg, record not postable, output-limit stop, stale tree) goes to plan-order at once as ONE `LESSON <what> <evidence>` line naming every occurrence; plan-order defines the root-cause keeper item (rule 3.56 (2)), never a second lane-by-lane ruling.
1i. **Repos in sync (OPERATOR D-5658).** A routerctl or L1 line that a main checkout is behind origin main goes to the host L1 in the same beat: clean = fast-forward, changed or diverged = save to a branch then update; nothing is discarded; no worktree from a stale base.
1j. **Batch check (rule 3.56 (1), D-6357).** A writer run (spawner record role implement) that holds one item while related ready items of the same files or capability exist is a LESSON to plan-order the same beat; so is a writer that pushes without a review file on its final head.
2. **Verify before reporting** (claims are not evidence). Read-only only:
   - merged? In the item's configured checkout: `git fetch -q && git merge-base --is-ancestor <sha> origin/main`
   - CI: `gh run view <id> --json conclusion,headSha`
   - gateway: ask the gateway lane for its configured read-only drift check (apply stays with the gateway's owning L1, DECISIONS 4.5).
   - a transcript claim: grep the cited file for the first occurrence line.
3. **Route**:
   - execution: SendMessage to the owning L1 (`fleet-name <role>`), one message that holds all its items.
   - gateway steps: the gateway's owning L1 (DECISIONS 4.5).
   - server, tailnet, firewall: the remote-host L1 named in the fleet inventory.
   - Questions: skill `route-question`.
4. **Log**: a verified outcome goes into docs/decisions-log.md (D-NNN, newest first) and, if it changes a rule, into DECISIONS.md, a CHANGELOG.md line, and the card line it changes. Run `bin/sync-memory-graph --load`. Commit and push.
5. A classifier refusal is never relayed or worked around. It becomes an OS-NN step with the exact line for the operator to type in the acting session.
