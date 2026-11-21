---
name: router-launch
description: Use when an L1 sends `L2-LAUNCH <name> <brief> <cwd>`, `L2-DONE <name>`, or "handoff ready" for a recycle. Launches, stops and recycles Claude Code sessions for the fleet (agents never launch sessions, D-158).
---

Source `router/config` in the router data checkout. Resolve `bin/`, `ops/` and
`HANDOFF.md` under `ROUTER_HOME`. Read the fleet inventory and the lane's
launch record for host placement and interactive session ownership.

> **Target architecture = the rethink design (operator D-1834):** read the design and operator answer rounds cited by the D-1834 keeper decision. Goal: as flat as possible, least Claude: no persistent L0/L1/L2 LLM sessions; keeper + Temporal + Rust worker + code accepts; Claude only as attended planner and on-demand judge; one card per worker session on free or subscription legs. The text below is transitional operation, never the plan.

# Launch, stop, recycle sessions

## Hierarchy is mandatory (D-971)
Every L1 launch or relaunch prompt starts with: "Invoke the skill unattended-orchestration before anything else; apply the depth rule as in force (no Claude L2 sessions, D-5193; workers, seats and research runs only through the plangraph spawner route table, D-2749; L1 holds plan graph, merges, designs and live steps; writers get batches of related items, one writer session per batch with its own review loop, via the agent-neutral writer.sh/review.sh, rule 3.56; lessons go to plan-order as `LESSON <what> <evidence>`). Reply 'SKILL-READ unattended-orchestration' with the 3 rules you apply." The skill is the plangraph copy under `skills/` in the plangraph checkout, linked at the user skills folder; the spawner MCP is the plangraph one configured for the lane (OPERATOR D-5648, D-5668, D-5671). L1 models: `claude-opus-5-5` low or `claude-sonnet-5-5` xhigh by role (D-5658).

## Quiet fleet (D-1364, D-1377)
Every launch prompt says: no recurring cron in any session (wait via one background command that exits); keep a card of at most 40 lines next to the handoff, refreshed before 230k, then `CARD-READY <path>` to the router; messages only for BLOCKED, DONE, GO, incident, launch; no ALIVE or ack messages (check the job state yourself). The router sends no acks either.

## L2-LAUNCH <name> <brief> <cwd>
1. Read the brief. Check: write scope, "no push/merge", "stop on denial", budget, TTL. If a rule is missing, ask the L1 to add it.
2. New cwd? Back up the active Claude user configuration to the scratchpad, then set `projects[<cwd>].hasTrustDialogAccepted = true`.
3. For the raw fallback, load `STYLE` from `ops/terse-style.txt` under `ROUTER_HOME`. Launch with the standard wrapper: `bin/fleet-launch <name> <cwd> sonnet high <prompt-file> --mcp <project .mcp.json> --parent <L1>`. It adds the lean profile (rule 4.02, D-3642: tools Artifact, Workflow, SendFeedback, ReportFindings off, `--settings ops/fleet-settings.json`, terse style) and writes the launch record. The raw command is the fallback and must carry the same `--settings` and `--disallowed-tools Artifact,Workflow,SendFeedback,ReportFindings,AskUserQuestion`:
```
cd <cwd> && claude --bg --strict-mcp-config --mcp-config <project .mcp.json> [<extra>.mcp.json] \
  --remote-control <name> -n <name> --model sonnet --effort high --permission-mode bypassPermissions --append-system-prompt "$STYLE" \
  "You are <name>, an L2 phase orchestrator launched by the router on <L1>'s request (D-285, interactive). Read your brief <brief> and execute it exactly; report only to <L1> via SendMessage. The operator may attach; answer them directly."
```
3b. **MCP config independent of cwd:** use fleet-launch's default MCP config, `ops/mcp-fleet-abs.json` under `ROUTER_HOME`, or set `FLEET_MCP_CONFIG` for the lane.
4. Tell the L1 the session id. Add `<name> <id>` to the HANDOFF "Live" line. Commit.

## L2-DONE <name>
`claude stop <id>`, remove it from the HANDOFF Live line, CHANGELOG line, commit. Never `claude rm`.

## Recycle an L1 ("handoff ready, no L2 running")
1. Only when NO in-session L2 runs (an in-session subagent dies with its L1).
2. `claude stop <old-id>`.
3. Relaunch at once with `bin/fleet-launch`, using the lane's configured cwd, model, effort and prompt file, and a prompt that names the handoff SECTION. No project-specific launch scripts.
4. Let fleet-launch record the launch for ctx-watchdog.
5. Update the session id in HANDOFF §3 and the state card. Log a D-row.

## Sessions on other hosts
You cannot launch there. Give the operator an exact OS-NN line: stop command, launch command with `--remote-control <name> -n <name>`, first prompt. Keep the name (the fleet messages by name).

## Interactive sessions you did not start
Stop only after their handoff landed and the operator agreed; they are not `--bg`, so use `kill -TERM <pid>` of `claude --resume <name>`.
