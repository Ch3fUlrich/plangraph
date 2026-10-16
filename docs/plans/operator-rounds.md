<!-- Imported verbatim (operator order 2026-10-16). Source: private repo Ch3fUlrich/routing, branch rethink-setup @ 6ff782a, path docs/plan/rethink/operator-rounds.md.
     Body below the marker is byte-identical; sha256(body) = ff41ee5ce25bd193b7c99c436924584ba450299eac3fee5f952e09875aad8eb7.
     Contains site strings: the PG-PUBLIC-READY scrub applies before any public flip. Do not edit; corrections go in a new file. -->
<!-- verbatim below -->
# Operator rounds — setup rethink (D-1285), verbatim log

Order D-1292 (operator via routing-00): every operator answer and pasted brief is written here VERBATIM as it arrives, then committed.
Full pasted brief (3 parts): `/home/s/code/routing-worktrees/rethink-setup/docs/research/rethink-2026-10-15/00-operator-brief.md`.
gen-2 conversation: `/home/s/code/routing-worktrees/rethink-setup/docs/plan/rethink/gen2-conversation.md`.

## Round 1 (gen-1, 2026-10-15, AskUserQuestion) — verbatim answers
- "Which outcome should the first milestone of the new setup prove?" = "Cost cut and progress speedup first, by perfectly orchestrating agents"
- "What role should Claude (Max 20x) have in the target setup?" = "Planner + judge only (Recommended)"
- "How much attended time can you give per day in the target state?" = "30-60 min/day attended"
- "How many projects should run in parallel in the target state?" = "1-2 at first, then widen (Recommended)"

## Round 2 (gen-1, 2026-10-15, AskUserQuestion) — verbatim answers
- "What happens to the running fleet (fixes-L1, server-L1, L2s) while the new setup is built?" = "Slim down, finish current lanes and only work on specific urgent parts, when i directly wirte into a chat what to do"
- "Which project should the new pipeline run first (milestone 1)?" = "Build itself (Recommended)"
- "Monthly cap for pay-as-you-go legs (beyond subscriptions, trial credits and free tiers)?" = "currently the main driver should be subscriptions :mimo and gemini (after a reset of the rate limit on friday evening), and free tiers: ovh (low amount of credits left), openrouter, opencode zen free, other free providers. Also consider to use the free "jev" model from e.g. opencode-zen/jev-1.13-free, pol/typesafe/jev-1.13, workstation-ollama-jev (not installed yet), server-ollama-jev (not installed yet). Do not use deepseek only if really needed, since it is a pay-as-you-go"
- "Should every unattended task be required to carry a deterministic check (test/command) before a worker may start?" = "Yes, no check = attended (Recommended)"

## Round 3 (gen-2, 2026-10-15) — as recorded by gen-2 (quotes verbatim where gen-2 quoted)
- Meta: "Do you still have enough context ... without loosing the important parts?"; order: "Ensure that you will not restart early, since we are in planning mode, which is crucial."
- Q-A architecture: not answered; operator pasted the other agent's Domain 5 + Part III again and asked to evaluate it and ask again (evaluation: `docs/research/rethink-2026-10-15/E1-eval-domain5-partIII.md`).
- Q-B engine: "i have no knowledge about temporal, but it is planned to have significantly more projects to run at the same time. the 1-2 projects are just for now, to test and continue." + "It seems like you lost some detailed information, which is not okay."
- Q-C runner language: Rust now.
- Q-D control-plane host: coding.vm now (keeper + runner scheduler + UI); workers stay on the workstation (D-437).

## Round 4 (gen-1 resumed, 2026-10-15)
- "Which target architecture should the plan build? (Your answer to Q-A, re-asked after the Domain 5 / Part III evaluation)" = "A: state + code (Recommended)" (preview: plangraph keeper DB + inbox + UI; runner/engine code 0 tokens; planner per phase exits; workers 1 card/session free/sub legs on workstation; reviewers phase-level 2 families; experts short-lived with context pack + omnigraph; no persistent L0/L1/L2 LLM sessions)
- "Execution engine under the plan graph: Temporal ... Which way?" = "What would the own implementation provide, what the temporal "durable execution" server does not? what should be the reason to test our home-grown version? Wouldn't it be better to improve the Temporal setup by participating instead of building our own?"
- "Keeper database (plan graph store) for the new runner, given Rust, many projects later, and concurrent writers?" = "Postgres in Docker on coding.vm (Recommended)"
- "gen-1 is back with its full context ... Is anything you told gen-2 still missing from the log?" = "Nothing missing"

## Round 5 (gen-1 resumed, 2026-10-15)
Context given before the round: own runner = one store/one UI/no extra service/full control; Temporal = proven timers, retries, heartbeats, signals, cancel, per-host queues, history UI; worker + projection needed either way; Temporal costs RAM, determinism rules, young Rust SDK. Recommendation: adopt Temporal, 1-day validation, own runner only as fallback.
- "Engine decision after the comparison above?" = "Temporal + 1-day validation. If RAM is not enough put it on the cloud.vm (plenty of ram) by messaging the "routing" agent, which should startup a "server" agent if it is not already running and delegate the task and spin up. How will it be possible to view the progress of the tasks etc.? Does "temporal" has a web ui or will this be represented inside our own web ui, similar like the omnigraph-ui knowledge graph which should also be integrated into the plangraph page?"
- "Where should the agent worker / spawner code live (it becomes the Temporal worker)?" = "plangraph repo and also move the agent skills into it, since everything related to agents and the orchestration should be inside. Do not move the server mcp if it is inside the the plangraph repo, since it belongs to "server""
- "Decisions ledger: how should decisions be recorded in the new setup?" = "Keeper table, operator-only rows (Recommended)"
- "Review policy for unattended work (operator rule kept: every task has a deterministic check)?" = "Phase-level, risk-scaled (Recommended)"

## Operator message (mid-turn, 2026-10-15, verbatim)
"I noticed, that omniroute now provide the following useful parts, which will make it easier to develop the system. The agent skill inside the website "dashboard/agent-skills":
1. mcp server: https://omniroute.ohje.ooguy.com/api/mcp/sse
2. a2a server: https://omniroute.ohje.ooguy.com/.well-known/agent.json
3. Memory: https://omniroute.ohje.ooguy.com/dashboard/memory
4. API endpoints (https://omniroute.ohje.ooguy.com/dashboard/endpoint) via:
   1. Cloud cmniroute
   2. tailscale funnel"

## Round 6 (gen-1 resumed, 2026-10-15)
Context given: Temporal has its own web UI (separate container: runs, step history, retries, timers); proposal = one plangraph page with tabs + per-run deep links into Temporal UI; omnigraph viewer as a tab.
- "How should the plangraph page show execution and knowledge?" = "One page, tabs + deep links (Recommended)"
- "Which MCP servers move where? (my reading of 'do not move the server mcp ... it belongs to server')" = "Infra MCP -> Server, spawner -> plangraph (Recommended)"
- "Which worker harnesses should the runner support (Claude = planner + judge only)?" = "Same + keep OpenHands"
- "Decision model (Jev) for task routing/readiness?" = "Rules + free Jev in shadow (Recommended)"

## Round 7 (gen-1 resumed, 2026-10-15)
- "Who builds the bootstrap (Temporal validation, keeper on Postgres, Rust worker MVP, schema additions) before the pipeline can build itself?" = "Build L1 + Sonnet subagents (Recommended)"
- "Keeper code path (keeper = Postgres per your answer; today Python 11.7k LOC on SQLite, 1,871 tests; Rust is the end state)?" = "Port Python keeper to Postgres now (Recommended)"
- "Fleet work superseded by this plan (D-909 fold lanes, AO-ROUTER-PUBLIC, ROUTING-INTO-AUTOOS, LAYERED-PLANNING-MOVE, AO-SKILL-INTERFACE, opencode L0/L1 lanes, new gate/ledger tooling)?" = "Cancel superseded, freeze tooling (Recommended)"
- "md-sync (markdown -> plan graph bridge) has been dead since 10-13 08:00Z, with no owner. Until the decisions table replaces it:" = "Re-arm now as stop-gap (Recommended)"

## Round 8 (gen-1 resumed, 2026-10-15)
Context given: W7 findings (MCP ~110 tools, no scope enforcement on live; A2A = routing wrapper; Memory = prompt injection/leak risk; Cloud uploads keys; Funnel = public exposure without Authelia).
- "Which OmniRoute features should the plan adopt?" = "MCP read-only pilot only (Recommended)"
- "On which OS should the Rust worker run on the workstation?" = "WSL2 Linux (Recommended)"
- "Milestone 1 success targets (pipeline builds its own next phase), measured against today's baseline?" = "Ambitious (Recommended)"
- "plangraph web UI framework for the new tabs (today: static HTML/JS, 8.5k LOC, rendered every 5 min; D-907 named Svelte)?" = "Svelte now for the all pages and tabs, therefore also convert"

routing-00 reply (2026-10-15): (1) md-sync re-arm and (2) tooling freeze routed to fixes-L1 as D-1297; (3) decisions logged as plan decisions. Batches: fixes-L1 (acting plangraph owner) applies data/updates yaml batches to the keeper (examples plangraph-data e5ceee2, fb9624a, 0335178), independent of md-sync; send routing the batch path + sha, routing judges and forwards to fixes-L1.

## Round 9 (design section approval, 2026-10-15)
- "Sections 2-3: architecture, component owners, data model — approve?" = "provide a proper path, so i can open the markdown files inside claude code desktop interface by clicking the link"
- "Sections 4-6 ..." = "[No preference]"; "Sections 7-9 ..." = "[No preference]"; "Sections 10-12 ..." = "[No preference]"

## Round 10 (design approval, 2026-10-15, verbatim)
"I approve all phases, but move the "agy, qodercli" parts into "P2 M1 dogfood" phase, since the subscription to

1. use gemini-3.8-flash and sonnet-5.5 is only possible via the antigravity (agy) acp
2. qwen-3.8-flash model is fully free while the remaining credits of the qoder subscription are remaining.
   1. to save mimo credits ensure that first qwen-3.8-flash is used (until rate limits) before mimo-v2.6-flash is used."

## Round 11 (2026-10-15, verbatim)
Context given: open question whether M1 keeps the speed target added after review (median card wall time ready->merged <= 50% of baseline lane median).
"yes, keep the speed target. I will now be not reachable, so you will be the one that should ensure that the project is going forward. keep an eye on the progress and ensure the project continues when it is stuck at some point"

## Round 12 (2026-10-15, verbatim)
"Stop watching"
