# RT-P0-TRIAGE: per-item table (448 open items, snapshot 2026-10-16 20:34Z)

| key | verdict | target | reason | live dependents | note |
|---|---|---|---|---|---|
| autoos:ACK-PROBE-BUDGET | INDEPENDENT | - | Gateway/registry probe budget per reasoning combo; audit gap, unrelated to the rethink phases. | routing:PH-AUDIT-1002 | - |
| autoos:AIPRO-LIMITS | INDEPENDENT | - | Research on Gemini key vs AI Pro limits; still informs leg choice (D7, D25) but is not rethink work. | routing:PH-AUDIT-1002 | - |
| autoos:AO-400LOC | INDEPENDENT | - | Generic file-size ratchet and CI gate; still valid (the Rust crate also keeps files <= 400 lines). | routing:ADOPT-400LOC-ROUTING, routing:ROUTE-400LOC, server:ADOPT-400LOC-CHAPTARR, server:ADOPT-400LOC-SERVER | deps AO-L2-PRODTEST and AO-L2-SKILL are cancelled in this chunk: drop those edges |
| autoos:AO-ADMISSION-OC-SHARED | COVERED | autoos:SPAWN-USES-PLACEMENT | Folded into the placement API (orch-setup S22, section 12.5); its done-criteria pass there. | autoos:PH-AO-FLEET | re-point PH-AO-FLEET to SPAWN-USES-PLACEMENT |
| autoos:AO-APIDOCS | INDEPENDENT | - | api-keys.md refresh from the registry; docs chore unrelated to the rethink. | autoos:PH-AO-FLEET | - |
| autoos:AO-APPLY-CLUSTER-013 | INDEPENDENT | - | Omnigraph 0.13 apply/compact scripts; infra ops, unrelated to the rethink. | autoos:PH-AO-FLEET | - |
| autoos:AO-AUTO-FANOUT | INDEPENDENT | - | Gateway auto/* fan-out cap and restart loop; OmniRoute fault, still valid and independent. | autoos:PH-AO-FLEET | - |
| autoos:AO-AUTOFIX | CANCEL | plangraph:RT-P1-REVIEW | Fix-task on failure/review is replaced by design 4.5 retry budget + park and 4.7 verdict fix cards in the phase. | routing:PH-FLEET-1003 | - |
| autoos:AO-AUTOOS5-REBUILD-DRIFT | INDEPENDENT | - | Gateway image tag drift and rebuild hold; ops, independent (not the combo drift of RT-P0-OR-DRIFT). | autoos:PH-AO-FLEET | - |
| autoos:AO-BZL-TARGET-TIMEOUT | INDEPENDENT | - | In progress (in_review). targetTimeoutMs on bzl combos overlaps class-combo targetTimeoutMs in RT-P1-CLASS-COMBOS. | routing:PH-OPPLAN-1012 | in progress |
| autoos:AO-CHANGELOG-DOCS | INDEPENDENT | - | check-changelog-docs + .docs-map repo hygiene; generic and valid. | routing:ROUTE-CHANGELOG-DOCS, server:ADOPT-CHANGELOG-SERVER | - |
| autoos:AO-CLAUDE-SEAT-BASH | INDEPENDENT | - | Review seats must run tests headless; still needed for cross-family review (design 4.7). | autoos:AO-L2-PRODTEST | dependent AO-L2-PRODTEST is cancelled |
| autoos:AO-COMBO-FREE-FIRST | INDEPENDENT | - | Free-first combos with provider cooldown overlap RT-P1-CLASS-COMBOS but also fix groq/bazaarlink errors; not cleanly superseded. | routing:PH-OPPLAN-1012 | unsure: partial overlap with autoos:RT-P1-CLASS-COMBOS (cooldown, leg order) |
| autoos:AO-CTX-RECYCLE-ALLHOSTS | COVERED | plangraph:SU-HOOK-CTXGUARD + plangraph:SU-LAUNCH | Operator ruling S19: folded into SU-HOOK-CTXGUARD (first_ts, ready --card, per-host install) and SU-LAUNCH. | routing:PH-OPPLAN-1012 | routing:PH-OPPLAN-1012 drops the edge (gated by PH-ORCH-GUARDS) |
| autoos:AO-FREE-PRIVACY-CLASS | INDEPENDENT | - | PUBLIC-ONLY privacy class on free legs; spawner/registry guard, still needed and independent. | autoos:PH-AO-FLEET | - |
| autoos:AO-GATE-TK | INDEPENDENT | - | Keeper run-record verifier for task_key runs; interim spawner-to-keeper path stays until the Rust worker replaces it. | routing:ROUTE-PG-PRODTEST | unsure: PGSSOT chain (TASKKEY/WRITEBACK/GATE-TK) may fold into worker events at RT-P1-WORKER |
| autoos:AO-GROK | INDEPENDENT | - | Temporary grok combo; gateway config chore, independent. | autoos:PH-AO-FLEET | - |
| autoos:AO-GW-HEAVY-INFLIGHT | INDEPENDENT | - | Measure gateway heavy in-flight saturation; gateway tuning, independent. | routing:PH-OPPLAN-1012 | - |
| autoos:AO-HAIKU | INDEPENDENT | - | Haiku route in the spawner; cost-saving routing, not tied to a rethink phase. | autoos:AO-HAIKU-EXPLORE, autoos:AO-HAIKU-GW, routing:HAIKU-PROPAGATE | unsure: Haiku is a Claude-family leg, check against D2 |
| autoos:AO-HAIKU-EXPLORE | INDEPENDENT | - | Haiku for read-only explore subagents; not tied to a rethink phase. | routing:SETUP-IMPROVEMENT-PLAN | unsure: Claude-family leg vs D2 |
| autoos:AO-HAIKU-GW | INDEPENDENT | - | Optional Haiku via the gateway; independent. | routing:SETUP-IMPROVEMENT-PLAN | unsure: Claude-family leg vs D2 |
| autoos:AO-HOSTMIRROR | INDEPENDENT | - | Spawner preflight combo check; valid while the AutoOS spawner is used. | autoos:AO-SKILLRULES, autoos:PH-AO-FLEET | - |
| autoos:AO-JULES-PIPELINE | INDEPENDENT | - | Automatic Jules proposal pipeline through existing gates; product work independent of the rethink. | routing:PH-OPPLAN-1012 | - |
| autoos:AO-L2-PRODTEST | CANCEL | design D9 / section 2 | Test of the interactive L2 phase layer; L2 sessions are replaced by Temporal card workflows (D9, section 2 "What disappears"). | autoos:AO-400LOC, autoos:PH-AO-FLEET, routing:ROUTE-L2-LAYER | dependents AO-400LOC, PH-AO-FLEET, routing:ROUTE-L2-LAYER need re-point |
| autoos:AO-L2-SEAT-INTEGRITY | INDEPENDENT | - | Seat re-seat after rebase and family resolution still matter for review, but the L2 brief template part is moot. | autoos:AO-L2-PRODTEST, autoos:AO-L2-SKILL | unsure: mixed; L2 template part dies, seat integrity part may stay; dependents AO-L2-PRODTEST/AO-L2-SKILL cancelled |
| autoos:AO-L2-SKILL | CANCEL | plangraph:RT-P4-SKILLS-REWRITE | L2 skill/layers text is replaced by the 150-line index plus role cards (RT-P4-SKILLS-REWRITE); L2 layer gone (D9). | autoos:AO-400LOC, autoos:AO-SKILLS, routing:ROUTE-L2-LAYER | dependents AO-400LOC, AO-SKILLS, routing:ROUTE-L2-LAYER need re-point |
| autoos:AO-L2-SMALLFIX-RULE | CANCEL | plangraph:RT-P4-SKILLS-REWRITE | Prose rule about L2 small fixes; L2 layer dies (D9) and prose rules become code or are deleted in RT-P4-SKILLS-REWRITE. | plangraph:PG-L2-SMALLFIX-RULE, routing:PH-OPPLAN-1012 | dependents plangraph:PG-L2-SMALLFIX-RULE, routing:PH-OPPLAN-1012 need re-point |
| autoos:AO-LAGUNA-SEAT-CAP | INDEPENDENT | - | Seats tool-less and diffs split under the 262k cap; matches the no-truncation review rule (4.7), still valid. | autoos:PH-AO-FLEET | - |
| autoos:AO-LEDGER-DRYRUN | INDEPENDENT | - | Writer-ledger write bug in the ready gate; valid until the gate is replaced. | autoos:AO-L2-SEAT-INTEGRITY, routing:ROUTE-WRITER-OPS | unsure: ready-gate ledgers may die at the P4 cutover |
| autoos:AO-LENS-REVIEW | CANCEL | plangraph:RT-P1-REVIEW | Stacked review lenses by risk tier are replaced by phase-level risk-scaled review (D15, design 4.7, RT-P1-REVIEW). | routing:SETUP-IMPROVEMENT-PLAN | - |
| autoos:AO-MCP-CWD-INDEPENDENT | INDEPENDENT | - | autoos-agent MCP start must be cwd-independent; valid while that MCP is in use. | autoos:PH-MCP-ONE | - |
| autoos:AO-MIMO-WIRING | INDEPENDENT | - | MiMo v2.6 model sync and capability flags; mimo is a D7 subscription leg, still needed. | routing:PH-OPPLAN-1012 | - |
| autoos:AO-OCRUNTIME | INDEPENDENT | - | oc-runtime sources into tools/oc_runtime; may belong to the oc_l* lanes (design section 10 deletions) but not confirmed. | autoos:PH-AO-FLEET | unsure: could be part of the opencode L0/L1 lane stack deleted by autoos:RT-P4-DELETIONS |
| autoos:AO-OPENCODE-JAIL | INDEPENDENT | - | Sandbox fence for opencode legs; design risk table keeps jail and outside-path fence, also needed for workers. | autoos:PH-AO-FLEET | - |
| autoos:AO-OVH404-COOLDOWN-SCOPE | INDEPENDENT | - | Per-model cooldown scope in the gateway; independent fix, aligns with providerCooldown work. | routing:PH-OPPLAN-1012 | - |
| autoos:AO-PG-MCP-WIRE | COVERED | plangraph:SU-MCP | Superseded by SU-MCP (user-scope plangraph MCP entry) in the orch-setup batch; cancelled there. | routing:ROUTE-PG-SSOT | dependent routing:ROUTE-PG-SSOT is re-pointed to plangraph:SU-MCP |
| autoos:AO-PREPUSH-HOOK-GITENV-2 | INDEPENDENT | - | Prepush guard against fixture git identities; generic repo safety. | autoos:PH-AO-FLEET | - |
| autoos:AO-PRICE-GPTOSS120B | INDEPENDENT | - | Price row for gpt-oss-120b; registry data. | routing:PH-FLEET-1003 | - |
| autoos:AO-PROBE-397B-TOOLS | INDEPENDENT | - | Tool_calls probe of Qwen3.5-397B; registry data. | routing:PH-FLEET-1003 | - |
| autoos:AO-QODER-ROUTE | INDEPENDENT | - | Resolver route to qoder qwen first free leg; interim registry/resolver work overlapping RT-P2-QODER and RT-P2-LEGORDER. | autoos:PH-AO-FLEET, routing:PH-OPPLAN-1012 | unsure: overlap with plangraph:RT-P2-QODER / RT-P2-LEGORDER (D25); freeze may apply |
| autoos:AO-READY-MULTI-BRIEF | INDEPENDENT | - | Ready multi-brief fence union; blocks a merge today. Ready gate may be replaced later (4.2, 4.4). | autoos:AO-SPAWN-PATCH-ID, autoos:PH-AO-FLEET | unsure: ready-gate cap ceremony is on the deletion list (section 10) |
| autoos:AO-REGISTRY-COMBO-LEGS | INDEPENDENT | - | Fixes combos with no usable models on the workstation gateway; overlaps RT-P0-OR-DRIFT but fixes registry content. | routing:GATE-P2 | unsure: overlap with autoos:RT-P0-OR-DRIFT / RT-P1-CLASS-COMBOS |
| autoos:AO-REPORT-SCHEMA | INDEPENDENT | - | Schema for spawned-run DONE/ERROR records; spawner substrate item, valid until it moves. | routing:PH-FLEET-1003 | unsure: partly overlaps RT-P1-REVIEW verdict schema |
| autoos:AO-RESEARCH-GLM-CODING-PLAN | INDEPENDENT | - | Research on a GLM coding plan; spend decision input (D7), independent. | autoos:PH-AO-FLEET | - |
| autoos:AO-RETIRE-AGENT-SKILLS-CLONE | INDEPENDENT | - | Ops cleanup of the agent-skills clone; independent. | autoos:PH-AO-FLEET | - |
| autoos:AO-RETRY-GATES | INDEPENDENT | - | Spawner retry re-checks the daily gate and host admission; valid spawner fix. | autoos:AO-L2-PRODTEST | - |
| autoos:AO-REVIEW-OF-XCHECKOUT | INDEPENDENT | - | Review-of across checkouts; spawner fix, still valid. | autoos:AO-L2-SEAT-INTEGRITY | dependent AO-L2-SEAT-INTEGRITY |
| autoos:AO-ROLE-GATE | INDEPENDENT | - | Spawner role/depth gate for the L0-L3 hierarchy; may become moot but spawner stays until RT-P4. | autoos:AO-L2-SKILL, routing:ROUTE-L2-LAYER | unsure: depth gate tied to the layered hierarchy that D9 removes |
| autoos:AO-RUNNER-NO-SUBAGENTS | CANCEL | autoos:RT-P4-DELETIONS | Guard for the PowerShell handoff runner config; that runner stack is deleted (design section 10, RT-P4-DELETIONS). | autoos:PH-AO-FLEET | dependent PH-AO-FLEET: drop edge |
| autoos:AO-SKILLRULES | INDEPENDENT | - | Mixed: rule rewording (RT-P4-SKILLS-REWRITE scope) plus spawner exit.json metrics; not cleanly cancellable. | autoos:AO-SPLIT1, autoos:PH-AO-FLEET | unsure: skill-rule part is a fold candidate for RT-P4-SKILLS-REWRITE |
| autoos:AO-SKILLS | INDEPENDENT | - | Research/review/implement skills and role tool sets; likely overlaps RT-P4-SKILLS-REWRITE role cards but depends on cancelled AO-L2-SKILL. | routing:ROUTE-SKILLS | unsure: fold candidate for plangraph:RT-P4-SKILLS-REWRITE; drop AO-L2-SKILL edge |
| autoos:AO-SLOT-GRANT-TOKEN | COVERED | autoos:SPAWN-USES-PLACEMENT | Folded into the placement API SlotLease (orch-setup S22, 12.2); its done-criteria pass there. | autoos:PH-AO-FLEET, routing:LESSONS-ENFORCE | re-point PH-AO-FLEET and routing:LESSONS-ENFORCE to SPAWN-USES-PLACEMENT |
| autoos:AO-SPAWN-FANOUT | INDEPENDENT | - | Parallel research/implementer runs across providers in the spawner; still valid interim capability. | routing:PH-FLEET-1003 | - |
| autoos:AO-SPAWN-FENCE-PRERUN | INDEPENDENT | - | Enforce not_family and review_base before launch; fresh routed fix (2026-10-16), valid. | routing:LESSONS-ENFORCE | - |
| autoos:AO-SPAWN-KEY-PRESET | INDEPENDENT | - | Spawner auth bug with preset gateway key; valid fix. | autoos:PH-AO-FLEET | - |
| autoos:AO-SPAWN-PATCH-ID | INDEPENDENT | - | Patch-id records for ready multi-brief; backlog follow-up of AO-READY-MULTI-BRIEF. | autoos:PH-AO-FLEET | unsure: follows AO-READY-MULTI-BRIEF fate |
| autoos:AO-SPAWN-PIN-UX | INDEPENDENT | - | Pin plus not-family UX; low-priority spawner fix. | autoos:AO-L2-PRODTEST | dependent AO-L2-PRODTEST is cancelled |
| autoos:AO-SPLIT1 | INDEPENDENT | - | Split autoos-agent.py by concern; helps the spawner move to plangraph and the 400-line rule. | autoos:PH-AO-FLEET | - |
| autoos:AO-SYNC-PARTIAL-PULL-RESTORE | INDEPENDENT | - | Omnigraph sync restore after partial pull; infra, independent. | autoos:PH-AO-FLEET | - |
| autoos:AO-TASKKEY | INDEPENDENT | - | Spawner task_key fetch/claim check; interim spawner-to-keeper path until the Rust worker. | autoos:AO-WRITEBACK, routing:ROUTE-PG-PRODTEST | unsure: PGSSOT chain may be superseded by worker once RT-P1-WORKER lands |
| autoos:AO-TERSE | INDEPENDENT | - | Terse brief prefix for spawned runs; aligns with token hygiene (design 8), valid. | autoos:AO-SKILLRULES, routing:PH-FLEET-1003 | - |
| autoos:AO-TOOLHOME-SIZE | INDEPENDENT | - | Per-run toolhome 540 MB cap; spawner hygiene. | autoos:AO-L2-SEAT-INTEGRITY | dependent AO-L2-SEAT-INTEGRITY |
| autoos:AO-VERDICT-PLAIN | INDEPENDENT | - | Minor gate-parser fixes for seat verdict strings; valid until the JSON verdict schema (RT-P1-REVIEW) replaces it. | routing:PH-OPPLAN-1012 | unsure: string-matched verdicts are replaced by JSON schema in 4.7 |
| autoos:AO-VIDEO-GATE | INDEPENDENT | - | Monthly video spend gate in Open WebUI; independent. | routing:SETUP-IMPROVEMENT-PLAN | - |
| autoos:AO-WORKST-RUNNER-POOL | CANCEL | plangraph:RT-P1-WORKER | Pool of workstation runner sessions for L2 dispatch is replaced by the Rust worker in WSL2 plus placement (D11, D22, S23). | routing:PH-OPPLAN-1012 | dependent routing:PH-OPPLAN-1012 needs re-point |
| autoos:AO-WRITEBACK | INDEPENDENT | - | Spawner record_run writeback to the keeper; interim until worker events/projector exist. | autoos:AO-GATE-TK, routing:ROUTE-PG-PRODTEST | unsure: PGSSOT chain; dependents AO-GATE-TK, routing:ROUTE-PG-PRODTEST |
| autoos:AO-WRITER-FAMILY | INDEPENDENT | - | Writer family resolved and one family vocabulary; needed for the cross-family fence. | routing:PH-FLEET-1003 | overlap with FAMILY-TRACK |
| autoos:AO-WS-OVH-PROBE | INDEPENDENT | - | Workstation OVH probe; data task. | routing:PH-FLEET-1003 | - |
| autoos:AO-WSCONFIG | INDEPENDENT | - | Workstation central-first with local fallback config; independent. | autoos:PH-AO-FLEET | - |
| autoos:AUTOOS-UI-PROFILES | INDEPENDENT | - | AutoOS web UI usage profiles; AutoOS product work. | routing:PH-AUDIT-1002 | - |
| autoos:BASHGUARD-ORCH-HARDEN | INDEPENDENT | - | Hardens bash-guard orchestrator mode; role may vanish with oc_l* lanes, but guard path logic may be shared with workers. | routing:PH-FLEET-1003 | unsure: orchestrator mode dies with opencode L0/L1 (section 10); bash-guard Rust port is plangraph:SU-HOOK-BASHGUARD |
| autoos:BAZAARLINK-AUTODISABLE | INDEPENDENT | - | OmniRoute auto-disable root cause; gateway fault. | routing:PH-AUDIT-1002 | - |
| autoos:CHEAPEST-PROVIDER | INDEPENDENT | - | Cheapest-provider rule for paid legs, pending a decision; independent. | routing:PH-AUDIT-1002 | - |
| autoos:CI-LOCAL-FALLBACK | COVERED | routing:RT-P1 | Already a dependency of the RT-P1 gate. | routing:RT-P1 | - |
| autoos:CI-RUNS-ON-POOL | COVERED | routing:RT-P1 | Already a dependency of the RT-P1 gate. | routing:RT-P1 | dep CI-WORKST-RUNNER is in progress; public repo CI on GitHub-hosted per S18 |
| autoos:CI-SMART-SELECT | INDEPENDENT | - | In progress (claimed). | routing:RT-P1 | in progress; already dep of routing:RT-P1 |
| autoos:CI-SPEED-WORKST | INDEPENDENT | - | In progress (in_review). | routing:RT-P1 | in progress; already dep of routing:RT-P1 |
| autoos:CI-WORKST-RUNNER | INDEPENDENT | - | In progress (in_review). | autoos:CI-RUNS-ON-POOL, plangraph:CI-RUNS-ON-WORKST, routing:RT-P1 | in progress; already dep of routing:RT-P1; dependent plangraph:CI-RUNS-ON-WORKST is re-scoped |
| autoos:COMBO-REFRESH | INDEPENDENT | - | Refresh all combos, drift 0 and served==pinned smoke on both gateways; overlaps RT-P0-OR-DRIFT scope. | routing:PH-FLEET-1003 | unsure: overlap with autoos:RT-P0-OR-DRIFT and RT-P1-CLASS-COMBOS; D-510 is old |
| autoos:COMBO-REFRESH-A4 | INDEPENDENT | - | Registry privacy filter fix; independent registry logic. | routing:PH-FLEET-1003 | - |
| autoos:CREDIT-18 | INDEPENDENT | - | Registry chain credit lane; generic ops. | autoos:PH-OPS-20261002 | - |
| autoos:DECIDE-RS-L3-COST | INDEPENDENT | - | Operator decision on gpt-oss grant stance and Qwen 397B cost cap; design D7 does not clearly settle it. | autoos:RS-L3-REDESIGN | unsure: dependent RS-L3-REDESIGN may be superseded (check chunk B) |
| autoos:DEVIN-BRIDGE | INDEPENDENT | - | Devin CLI bridge in the install catalog; AutoOS product work, not among the D17 harnesses. | routing:PH-AUDIT-1002 | - |
| autoos:DOCS-OMNIROUTE | INDEPENDENT | - | Document OmniRoute traps; doc chore. | autoos:PH-OPS-20261002 | - |
| autoos:FA-3 | INDEPENDENT | - | FLEET-AGENTS-2 minors; generic ops follow-up. | autoos:PH-OPS-20261002 | unsure: scope unclear from title |
| autoos:FAMILY-TRACK | INDEPENDENT | - | Spawner resolves family of combo pins for review-of fence; overlaps AO-WRITER-FAMILY. | routing:PH-FLEET-1003 | overlap with autoos:AO-WRITER-FAMILY; merge candidate |
| autoos:FLEET-STATUS-HOOK | INDEPENDENT | - | In progress (claimed). Interim Python hooks, ported to Rust by plangraph:SU-HOOK-FLEET. | routing:RT-P1 | in progress; already dep of routing:RT-P1 |
| autoos:G-KEEPER | CANCEL | design D9 / section 2 | Session keeper relaunching dead L1/L2 from handoffs is replaced by Temporal leases/retries; persistent sessions and recycle cycles disappear. | autoos:O3-GKEEPER-OPENCODE, routing:PH-FLEET-1003 | dependents O3-GKEEPER-OPENCODE (cancelled), routing:PH-FLEET-1003 need re-point |
| autoos:GEMINI38 | INDEPENDENT | - | Gemini 3.8 Flash only in registry/combos/pins; consistent with D25 and independent. | autoos:AIPRO-LIMITS, routing:PH-AUDIT-1002 | - |
| autoos:GROK-UPSTREAM-PR | INDEPENDENT | - | Upstream OmniRoute PRs; independent. | routing:PH-AUDIT-1002 | - |
| autoos:GWRETRY3 | INDEPENDENT | - | Gateway retry v3 rebase; gateway ops chain. | autoos:PH-OPS-20261002 | - |
| autoos:JULES-CLOUDAGENT | INDEPENDENT | - | Jules via OmniRoute Cloud Agents API; D21 excludes OmniRoute Cloud for planner MCP only, this is a leg wiring. | routing:PH-AUDIT-1002 | unsure: check against D21 (no OmniRoute Cloud) |
| autoos:LAUNCHER-CPU-GATE | INDEPENDENT | - | Launcher CPU/RAM gate and parallel cap overlap the placement rules (HostMemoryFloor, HostWorkerCap) but differ (load > 75 % refusal). | autoos:RUNNER-SCALE-20, routing:PH-FLEET-1003 | unsure: overlap with plangraph:PLACEMENT-RULES / autoos:SPAWN-USES-PLACEMENT; dependent RUNNER-SCALE-20 |
| autoos:LEAN-MCP | INDEPENDENT | - | Disable MCP servers in writer/seat opencode runs; matches design 8 minimal MCP surface, valid. | autoos:O5-LEAN-MCP-PILOT, routing:PH-FLEET-1003 | dependent O5-LEAN-MCP-PILOT is cancelled |
| autoos:MCP-REMOTE | INDEPENDENT | - | Remote spawn via host runner overlaps pull-based Rust workers (sections 4, 6) but the placement caller (S22) needs a remote spawn path today. | autoos:ORCH-L2-LAUNCHER, autoos:PH-MCP-ONE | unsure: overlap with T2-XMSG and Rust worker task queues; dependents ORCH-L2-LAUNCHER (cancelled), PH-MCP-ONE |
| autoos:MCP-SEAT-EVIDENCE | INDEPENDENT | - | Seat evidence collected inside spawn before sandbox cleanup; valid spawner/MCP work. | autoos:PH-MCP-ONE | dependent PH-MCP-ONE |
| autoos:MEMGUARD | INDEPENDENT | - | coding.vm 1 GiB memory watchdog; ops safety, separate from placement floors. | routing:PH-AUDIT-1002 | - |
| autoos:O1-OPENCODE-SESSIONS | CANCEL | design D9 / section 10 | One long-lived opencode L1 session per project is a persistent L1; replaced by state + Temporal (D9); opencode L0/L1 lanes are deleted. | autoos:O3-GKEEPER-OPENCODE, autoos:O5-LEAN-MCP-PILOT, autoos:O6-ROUTING-ON-OPENCODE, routing:PH-FLEET-1003 | dependents O3, O5, O6 (cancelled), routing:PH-FLEET-1003 |
| autoos:O2-FLEET-BUS-MCP | CANCEL | design section 6 | Cross-host session bus; replaced by hub-and-spoke via keeper + engine, status is state, no peer messaging (sections 2, 6). | autoos:O6-ROUTING-ON-OPENCODE, routing:PH-FLEET-1003 | dependents O6 (cancelled), routing:PH-FLEET-1003; answer delivery now Q-API-PUSH / PG-INBOX |
| autoos:O3-GKEEPER-OPENCODE | CANCEL | design D9 / section 10 | Session-keeper adapter for opencode L1 sessions; the sessions and G-KEEPER are removed (D9, section 10). | autoos:O6-ROUTING-ON-OPENCODE, routing:PH-FLEET-1003 | dependents O6 (cancelled), routing:PH-FLEET-1003 |
| autoos:O5-LEAN-MCP-PILOT | CANCEL | design D9 / section 10 | Pilot runs on an opencode L1 project session (O1) that is removed; worker MCP surface is covered by design 8 and SU-HARNESS-CFG. | autoos:O6-ROUTING-ON-OPENCODE, routing:PH-FLEET-1003 | dependents O6 (cancelled), routing:PH-FLEET-1003 |
| autoos:O6-ROUTING-ON-OPENCODE | CANCEL | design D9 / section 2 | Persistent routing-00 on OpenCode; the persistent router disappears (D9, section 2 "What disappears"). | routing:PH-FLEET-1003 | dependent routing:PH-FLEET-1003 |
| autoos:OMNIUPDATE | INDEPENDENT | - | OmniRoute update chain U1-U6; gateway ops with many dependents. | autoos:BAZAARLINK-AUTODISABLE, autoos:PH-OPS-20261002, autoos:T0-PAID, autoos:T1-NOISE, autoos:T4-HARDEN, autoos:WRITER-BENCH | - |
| autoos:OP-MANAGE-KEY | INDEPENDENT | - | Operator task: OmniRoute manage-scope key; unblocks drift export (RT-P0-OR-DRIFT), so still needed. | routing:GOAL | unsure: confirm RT-P0-OR-DRIFT needs this key |
| autoos:OP-WS-RELAUNCH | INDEPENDENT | - | Operator task to relaunch workst-L1-autoos, which still runs the orch-setup lanes; not clearly moot. | routing:GOAL | unsure: depends on D5 fleet slimming |
| autoos:OR-FREE-GUARD | INDEPENDENT | - | Test that OpenRouter legs are :free only; spend guard, valid. | routing:PH-AUDIT-1002 | - |
| autoos:ORCH-L2-LAUNCHER | CANCEL | design D9 / section 2 | Host launcher for L2 phase orchestrators; L2 sessions are replaced by Temporal card workflows and plangraph launch for attended sessions. | autoos:PH-MCP-ONE, routing:GOAL | dependents PH-MCP-ONE, routing:GOAL need re-point |
| autoos:ORCH-REVIEW-TIERS | CANCEL | plangraph:RT-P1-REVIEW | Risk-tier reviewer count is replaced by card risk tags plus phase review level (D15, design 4.3/4.7, RT-P1-REVIEW, RT-P1-PLANNER-KIT). | routing:GOAL | dependent routing:GOAL needs re-point |
| autoos:ORCH-V4-DOCS | CANCEL | plangraph:RT-P4-SKILLS-REWRITE | Docs for the L0-L3 hierarchy and skill levels are obsolete after D9; skills and docs are rewritten in RT-P4-SKILLS-REWRITE. | autoos:ORCH-L2-LAUNCHER, autoos:ORCH-REVIEW-TIERS | dependents ORCH-L2-LAUNCHER, ORCH-REVIEW-TIERS (both cancelled) |
| autoos:OVH-GUARD | INDEPENDENT | - | OVH concurrency and spend caps; valid spend guard, analogous to ProviderConcurrencyCap. | routing:PH-FLEET-1003 | overlap hint: PLACEMENT ProviderConcurrencyCap cites OVH-GUARD style caps |
| autoos:PH-AO-FLEET | INDEPENDENT | - | Gate with many live independent deps; cancel only if all deps are cancelled or covered, which is not the case. | routing:PH-FLEET-1003 | drop edges: AO-ADMISSION-OC-SHARED, AO-SLOT-GRANT-TOKEN (to SPAWN-USES-PLACEMENT), AO-RUNNER-NO-SUBAGENTS, AO-L2-PRODTEST |
| autoos:PH-MCP-ONE | INDEPENDENT |  | Gate over MCP-ONE phase; deps in chunk A and in this chunk (SKILL2MCP, T2-MCPLIFE, T2-ONEPATH) stay open. | routing:GOAL | gate: re-evaluate after merge; cancel only if all deps cancelled/covered |
| autoos:PH-OPS-20261002 | INDEPENDENT |  | Ops gate; deps are ops lanes (provider wiring, CI, zcode, bench) unrelated to the rethink and still open. | routing:GOAL |  |
| autoos:PH-ROUTESETUP | INDEPENDENT |  | Gate keeps open independent deps (AA-SCORES, GEMINI-PARAMS, NSCALE, OPENCODE-MODELS, ...) even though several RS lanes are cancelled. | routing:PH-FLEET-1003 | drop cancelled deps RS-L3-REDESIGN, RS-PHASE-GATES, RS-L2-RESEARCHER-1M, RS-SPAWNER-MCP, RS-CLEF-PILOT, RS-LIVE-SYNC, RS-LIT-TABLE |
| autoos:PH-WS-SHIFT | INDEPENDENT |  | Gate keeps WS-1, WS-3, WS-6 open; WS-2/WS-5 cancelled and WS-4 covered, so not all deps are gone. | routing:GOAL | drop WS-2, WS-5, WS-4 edges |
| autoos:PROBE-LEDGER-ATOMIC | INDEPENDENT |  | Small probe-ledger hardening (tmp + replace); unrelated to the rethink. | routing:PH-FLEET-1003 |  |
| autoos:PROV-WIRE-FREE | INDEPENDENT |  | Provider wiring of free legs into combos; free legs are used by the design leg order (D7, section 5). | autoos:JULES-CLOUDAGENT, routing:PH-AUDIT-1002 |  |
| autoos:PROVROUTE | INDEPENDENT |  | Provider routing registry chain; registry/combos stay (AutoOS keeps catalog/registry, design section 10). | autoos:GEMINI38, autoos:PH-OPS-20261002, autoos:PROV-WIRE-FREE |  |
| autoos:PUSHGATE-TIGHTEN | INDEPENDENT |  | opencode.jsonc push gate hardening; opencode stays a harness (D17), config still needs a safe push gate. | routing:PH-FLEET-1003 | unsure: worker confinement may later subsume it |
| autoos:QODER-FREE-LEG | CANCEL | D25; plangraph:RT-P2-QODER | Decision to use qoder qwen-3.8-flash as free leg already taken in D25 (section 5); adapter A/B is RT-P2-QODER. | routing:PH-AUDIT-1002 | re-point routing:PH-AUDIT-1002 to RT-P2-QODER or drop edge |
| autoos:ROUTE-FRESH | INDEPENDENT |  | Spawner substrate hardening (stale/dirty checkout refusal); substrate moves in RT-P4-SPAWNER, still useful until then. | routing:PH-FLEET-1003 |  |
| autoos:RS-AA-SCORES | INDEPENDENT |  | Registry data (AA score, cost per task) feeding leg order and routing; unrelated to what the rethink replaces. | autoos:PH-ROUTESETUP, autoos:RS-L3-REDESIGN, autoos:RS-MODELS-DOC |  |
| autoos:RS-CHANGELOG-GATE | INDEPENDENT |  | Repo push-gate for CHANGELOG lines; hygiene tooling not covered by the design. | autoos:PH-ROUTESETUP | unsure: fleet-tooling freeze (D20) may defer it |
| autoos:RS-CLEF-PILOT | CANCEL | D18; plangraph:RT-P5-JEV | L0 classification pilot: no persistent L0 (D9); rules decide, Jev runs in shadow (D18, RT-P5-JEV). | autoos:PH-ROUTESETUP | overlap with Jev shadow |
| autoos:RS-FUTURE-MODELS | INDEPENDENT |  | Docs section on future models; model research unrelated to the rethink. | autoos:PH-ROUTESETUP |  |
| autoos:RS-GEMINI-PARAMS | INDEPENDENT |  | OmniRoute gemini/vertex request params patch; the gateway stays (D21, section 5). | autoos:PH-AO-FLEET, autoos:PH-ROUTESETUP |  |
| autoos:RS-GEMINI38-LEGS | INDEPENDENT |  | Gateway combo adds agy + vertex legs; design reaches agy only via ACP (D25), so an agy gateway leg may conflict. | autoos:PH-ROUTESETUP, autoos:RS-NESTED-COMBOS | unsure: agy leg via OmniRoute vs D25 (agy only via ACP) |
| autoos:RS-L2-RESEARCHER-1M | CANCEL | autoos:RT-P1-CLASS-COMBOS | Role-named l2-researcher/lookup-128k combos replaced by window-class combos *-128k/*-256k/*-1m (section 5); no persistent L2. | autoos:PH-ROUTESETUP, autoos:RS-PHASE-GATES |  |
| autoos:RS-L3-REDESIGN | CANCEL | autoos:RT-P1-CLASS-COMBOS | L3 redesign split by context is exactly the window-class combo design (section 5); layer model dropped (D9). | autoos:PH-ROUTESETUP, autoos:RS-PHASE-GATES, autoos:RS-SPAWNER-MCP | its deps DECIDE-RS-L3-COST, RS-AA-SCORES: RS-AA-SCORES stays independent |
| autoos:RS-LEDGER-WIRING | INDEPENDENT |  | Probe-ledger wiring (probes record, check first); tooling hygiene independent of the design. | autoos:PH-ROUTESETUP |  |
| autoos:RS-LIT-TABLE | CANCEL | D9; research R1-R5/W1-W7 | Literature table on multi-level orchestration; architecture A (no persistent levels) decided with its own research (design sections 0, 1). | autoos:PH-ROUTESETUP | low risk; research only |
| autoos:RS-LIVE-SYNC | CANCEL | autoos:RT-P0-OR-DRIFT; plangraph:SU-HARNESS-CFG | Combos.json vs live drift check duplicates RT-P0-OR-DRIFT; opencode user config writing is SU-HARNESS-CFG. | autoos:PH-ROUTESETUP | duplicate |
| autoos:RS-META-KEY | INDEPENDENT |  | Operator key task (new Meta API key); unrelated to the rethink. | autoos:PH-ROUTESETUP | operator task |
| autoos:RS-MODELS-DOC | INDEPENDENT |  | models-proposed.md cleanup generated from the registry; docs, unrelated. | autoos:PH-ROUTESETUP, autoos:RS-FUTURE-MODELS |  |
| autoos:RS-NESTED-COMBOS | INDEPENDENT |  | Nested combos in OmniRoute (or upstream PR); gateway capability still useful for window-class combos. | autoos:PH-ROUTESETUP |  |
| autoos:RS-NOTIFY-L1S | INDEPENDENT |  | Operator approval of a relay text to other L1 projects; content unknown, may be moot once L1s retire. | autoos:PH-ROUTESETUP | unsure: operator task; may be moot (no persistent L1s, D9); operator confirm before any cancel |
| autoos:RS-NSCALE | INDEPENDENT |  | nscale model retirement mapping in registry; provider data. | autoos:PH-ROUTESETUP |  |
| autoos:RS-OC-KEY-GLM | INDEPENDENT |  | Operator key task for the glm-5.3-flash leg; provider ops. | autoos:PH-ROUTESETUP | operator task |
| autoos:RS-OPENCODE-MODELS | INDEPENDENT |  | Registry/ledger integration of opencode-native models; supports opencode harness legs (D17). | autoos:PH-ROUTESETUP |  |
| autoos:RS-PHASE-GATES | CANCEL | D9; autoos:RT-P4-DELETIONS | Moving L2/L3 then L1 onto OpenCode: opencode L0/L1 orchestrator lanes are deleted (section 10), no persistent L1/L2. | autoos:PH-ROUTESETUP |  |
| autoos:RS-PLAN-ROWS | INDEPENDENT |  | Plan rows 13/15/16/18 (L3 tool allowlists, spawn ban, interview template, relay envelope); mixed, some map to minimal tool surface. | autoos:PH-ROUTESETUP | unsure: relay envelope moot (section 6) but allowlists stay (section 8); split by owner |
| autoos:RS-RENAME-CI | INDEPENDENT |  | CI verdict check on a specific main sha; verification of finished work. | autoos:PH-ROUTESETUP |  |
| autoos:RS-SPAWN-TEMPLATES | INDEPENDENT |  | Spawner role templates with hotfix bypass; spawner substrate moves in RT-P4-SPAWNER, still valid interim. | autoos:PH-ROUTESETUP | unsure: role templates are L1/L2/L3 oriented |
| autoos:RS-SPAWNER-MCP | CANCEL | D9; design section 4 (keeper-side routing) | Spawn routing by l1/l2/l3 layer ids: layers vanish, routing is keeper code and worker executes the assigned target. | autoos:PH-ROUTESETUP | its dep RS-L3-REDESIGN also cancelled |
| autoos:RT-P0-OR-DRIFT | COVERED | routing:RT-P0 | Rethink item, already a dep of gate routing:RT-P0. | autoos:RT-P1-CLASS-COMBOS, routing:RT-P0 |  |
| autoos:RT-P1-CLASS-COMBOS | COVERED | routing:RT-P1 | Rethink item, already a dep of gate routing:RT-P1 (and plangraph:RT-P1-E2E). | plangraph:RT-P1-E2E, routing:RT-P1 |  |
| autoos:RT-P4-DELETIONS | COVERED | routing:RT-P4 | Rethink item, already a dep of gate routing:RT-P4. | routing:RT-P4 |  |
| autoos:RT-P4-INSTALLER | COVERED | routing:RT-P4 | Rethink item, already a dep of gate routing:RT-P4 (gains orch-setup deps per section 9). | routing:RT-P4 |  |
| autoos:RUNNER-SCALE-20 | INDEPENDENT |  | Workstation fair-share parallelism up to 20 runs; overlaps placement rules but is not in the named fold list. | routing:PH-FLEET-1003 | unsure: overlaps WorkstationUsableGuard/ProviderConcurrencyCap (orch-setup 12.2); fold candidate into SPAWN-USES-PLACEMENT |
| autoos:SKILL-TERSE-PEER | CANCEL | design section 6; plangraph:RT-P4-SKILLS-REWRITE | Terse-peer messaging rules for the old skill: communication rules are rewritten in design section 6, skill rewritten in RT-P4. | routing:PH-FLEET-1003 |  |
| autoos:SKILL2MCP | INDEPENDENT |  | MCP-ONE M6 skill lint (MCP calls only); spawn path stays interim, skills later rewritten in RT-P4-SKILLS-REWRITE. | autoos:PH-MCP-ONE | unsure: may fold into RT-P4-SKILLS-REWRITE lint; deps WS-1, EVTAG |
| autoos:SPAWN-LONGPATH | INDEPENDENT |  | Spawner --isolate Windows long-path bug; isolation moves with substrate in RT-P4-SPAWNER, bug fix stays valid. | routing:PH-FLEET-1003 |  |
| autoos:SPAWNER-F0-CWD | INDEPENDENT |  | Privacy fence: classify by cwd repo; fences are part of spawner substrate that moves (RT-P4-SPAWNER); safety fix still valid. | routing:PH-FLEET-1003 |  |
| autoos:T0-KEYS | INDEPENDENT |  | Operator key rotation and hygiene; ops, unrelated to the rethink. | routing:GATE-P0 | operator task |
| autoos:T0-PAID | INDEPENDENT |  | Disable deepseek/meta paid legs; design keeps deepseek as last-resort leg (D7), so partly conflicting. | autoos:T1-CLEAN, autoos:T1-COMBO3, routing:GATE-P0 | unsure: conflicts with deepseek-last leg order (section 5); deps WS-1, OMNIUPDATE |
| autoos:T0-SYNC | INDEPENDENT |  | State in_review: in progress. | routing:GATE-P0 | in progress |
| autoos:T0-ZEN1 | INDEPENDENT |  | Operator click to remove second Zen account (ToS); ops. | routing:GATE-P0 | operator task |
| autoos:T1-CLEAN | INDEPENDENT |  | Private-safe tier for privacy-sensitive cards; privacy routing still needed (risk tags, section 3). | server:T6-DETECT, server:T6-OWUI |  |
| autoos:T1-COMBO-LAYERS | CANCEL | autoos:RT-P1-CLASS-COMBOS | Combos grouped by L0-L2 sticky / L3 rotating layer replaced by window-class combos with sticky session per attempt (section 5). | routing:GOAL | dep T1-COMBO3 stays |
| autoos:T1-COMBO3 | INDEPENDENT |  | Combo contract C1-C7+C10 and combos v3 renderer; overlaps window-class combos but prerequisite of many open lanes. | autoos:ACK-PROBE-BUDGET, autoos:GEMINI38, autoos:OR-FREE-GUARD, autoos:T1-COMBO-LAYERS, autoos:T1-CTX, autoos:T1-LITELLM, autoos:T1-PEER, autoos:T1-PROV, autoos:T1-UPGRADE, autoos:T5-SYNC, autoos:TIER-CONTRACT, routing:GATE-P1, server:SEC-TIER-A | unsure: overlap with RT-P1-CLASS-COMBOS; consider making it a dep of that item |
| autoos:T1-CREDIT | INDEPENDENT |  | Burn trial credits before expiry; trial legs rank first in leg order, but its report goes to the L0 inbox. | routing:GOAL | unsure: overlaps governor RT-P1-GOVERNOR (provider quota); L0 inbox part moot |
| autoos:T1-CTX | INDEPENDENT |  | Live context truth for combos (/v1/models context_length); complements measured windows and class-combo floors (section 5). | routing:GATE-P1 | overlap RT-P1-CLASS-COMBOS contextRequirements; unsure whether it merges |
| autoos:T1-JEV | CANCEL | D18; plangraph:RT-P5-JEV | Jev routing-card experiment: design runs free Jev in shadow, logs outcomes, adopt/reject decision in RT-P5-JEV. | routing:GOAL | named overlap T1-JEV; carry the >=95% agreement / no unsafe upgrade threshold into RT-P5-JEV |
| autoos:T1-LITELLM | INDEPENDENT |  | LiteLLM standby gateway rendered from combos.json; gateway ops, design keeps OmniRoute but does not mention LiteLLM. | routing:GATE-P2 | unsure: standby may be obsolete |
| autoos:T1-MM | INDEPENDENT |  | Multimodal (stt/tts/image) inventory and combos; product work unrelated to the rethink. | routing:GOAL, server:OWUI-EXTRAS |  |
| autoos:T1-NOISE | INDEPENDENT |  | Cut OmniRoute connection-test pings; gateway ops. | routing:GOAL |  |
| autoos:T1-PEER | INDEPENDENT |  | Workstation OmniRoute peers the server gateway with loop guard; gateway infra, relevant for workstation workers. | autoos:T5-SYNC, routing:GATE-P2 |  |
| autoos:T1-PROV | INDEPENDENT |  | Probe provider keys into registry rows; provider data feeding legs. | autoos:CHEAPEST-PROVIDER, autoos:DEVIN-BRIDGE, autoos:PROV-WIRE-FREE, autoos:T1-CREDIT, autoos:T1-MM, routing:GATE-P2 |  |
| autoos:T1-UPGRADE | INDEPENDENT |  | OmniRoute upgrade with patch bundle re-verified; gateway ops. | routing:GOAL |  |
| autoos:T1-WEBTOOLS | INDEPENDENT |  | Self-hosted SearXNG MCP for research clients; tooling unrelated to the rethink. | autoos:T2-RESEARCHCARD, routing:GOAL |  |
| autoos:T1-ZEN | INDEPENDENT |  | opencode clients use own Zen key directly; Zen free is a free leg in the design (section 5). | routing:GOAL | near-duplicate of T4-ZEN |
| autoos:T2-BUDGET | COVERED | autoos:SPAWN-USES-PLACEMENT | Folded into the placement API by orch-setup S22 (rules HostWorkerCap, HostMemoryFloor, OverflowQueue). | autoos:T2-CUTRUN | re-point dependent T2-CUTRUN (itself cancelled) |
| autoos:T2-CANARY | INDEPENDENT |  | Secret canary fixture and transcript scan; security hygiene not covered by the design. | autoos:T4-FENCE |  |
| autoos:T2-COMPACT | INDEPENDENT |  | Compaction policy; re-pointed to wait on RT-P4-SKILLS-REWRITE by S19; design section 8 requires rules re-read after compaction. | autoos:TIER-CONTRACT, routing:GOAL | dep T2-SKILL now plangraph:RT-P4-SKILLS-REWRITE |
| autoos:T2-CUTRUN | CANCEL | design section 4.5 (usage_limit/outage, durable timers) | Cut-run detection and fresh-agent finish is Temporal retry, durable timers and exit classes; its budget dep is covered. | autoos:T2-WATCH |  |
| autoos:T2-LEAN | INDEPENDENT |  | --lean AND writer routing input vs refusal; spawner path remains interim, routing moves to keeper later. | routing:GOAL | unsure: keeper-side routing may subsume it |
| autoos:T2-MCPLIFE | INDEPENDENT |  | MCP server lifecycle hardening (timeout, reap, restart) of the interim autoos-agent MCP; needed while it runs. | autoos:PH-MCP-ONE, routing:GOAL | deps WS-1, EVTAG, T2-ONEPATH |
| autoos:T2-ONEPATH | INDEPENDENT |  | One select_combo path for CLI/MCP; interim consolidation, overlaps design 5 collapse of routing layers (RT-P4-DELETIONS). | autoos:PH-MCP-ONE, autoos:T1-JEV, autoos:T1-ZEN, autoos:T2-LEAN, autoos:T2-MCPLIFE, autoos:T2-RECORD, autoos:T2-TASKSESSION, routing:GATE-P1 | unsure: may be REPARENT to routing:RT-P4 with deletions; many dependents |
| autoos:T2-RECORD | INDEPENDENT |  | Run record fields and forged-review refusal; attempts table later projects this, interim record still needed. | autoos:QODER-FREE-LEG, autoos:T2-BUDGET, routing:GATE-P1 | dependents QODER-FREE-LEG (cancelled), T2-BUDGET (covered) |
| autoos:T2-RECORD-GAPS | INDEPENDENT |  | Run-record gaps for pinned or-* models; small spawner fix. | routing:GOAL |  |
| autoos:T2-RESEARCHCARD | INDEPENDENT |  | Research cards route only to web-capable clients; still a valid routing requirement. | routing:GOAL |  |
| autoos:T2-RULES | CANCEL | plangraph:RT-P4-SKILLS-REWRITE | Skill-rule evaluation mechanical->code is the P4 skills rewrite scope (design section 11 P4, orch-setup 7a). | autoos:T2-SKILL, routing:GATE-P2 | routing:GATE-P2 drops the edge per S19 |
| autoos:T2-SKILL | COVERED | plangraph:RT-P4-SKILLS-REWRITE | Cancelled as duplicate of RT-P4-SKILLS-REWRITE by S19. | autoos:T2-COMPACT, routing:GATE-P2 | T2-COMPACT re-pointed, GATE-P2 drops edge |
| autoos:T2-TASKSESSION | CANCEL | D9; design section 8 | Long-lived orchestrators contradict D9; one card per fresh worker session is already the design (sections 4, 8). | routing:GOAL |  |
| autoos:T2-WATCH | CANCEL | design section 4.5 (Temporal heartbeats, leases) | Heartbeat/watchdog and relaunch replaced by Temporal activity heartbeats and lease_lost retries; heartbeat crons disappear (section 2). | autoos:T2-RULES |  |
| autoos:T2-XMSG | CANCEL | design section 6; plangraph:Q-API-PUSH | Cross-host messaging replaced: status is state, hub-and-spoke via keeper, questions via Inbox, delivery via plangraph MCP. | autoos:T7-CONSOLE-MSG | named overlap; also PG-INBOX / RT-P3-PHONE |
| autoos:T2-XSECRET | INDEPENDENT |  | Secret exchange between devices (age keys); not addressed by the design, security infra. | autoos:T7-CONSOLE-MSG | dependent T7-CONSOLE-MSG cancelled |
| autoos:T3-CIGREEN-WIN | INDEPENDENT |  | CI green on the Windows/workstation runner; CI infra, adjacent to CI-WORKST-RUNNER in RT-P1. | autoos:PH-OPS-20261002 | overlap autoos:CI-WORKST-RUNNER / plangraph:CI-RUNS-ON-WORKST |
| autoos:T3-CLEAN | INDEPENDENT |  | Delete throwaway scripts, archive old handoffs; partially overlaps RT-P4-DELETIONS but broader. | routing:GOAL | overlap RT-P4-DELETIONS (diag scripts) |
| autoos:T3-PREPUSH | INDEPENDENT |  | Local-green pre-push gate in every worktree; dev hygiene. | autoos:T3-CLEAN, autoos:T3-PRINCIPLES, autoos:T3-SPLIT, autoos:T3-WIN |  |
| autoos:T3-PRINCIPLES | INDEPENDENT |  | coding-principles skill gains MUST rules; separate skill from unattended-orchestration. | routing:GOAL |  |
| autoos:T3-SCRUB | INDEPENDENT |  | Merge scrub-gap (tools/ in public-scrub scan); public-repo hygiene. | routing:GOAL |  |
| autoos:T3-SPLIT | INDEPENDENT |  | 400-line rule CI check; dev hygiene. | routing:GOAL |  |
| autoos:T3-WIN | INDEPENDENT |  | Windows CI job re-enable or local-green; CI infra. | routing:GOAL |  |
| autoos:T4-FENCE | INDEPENDENT |  | Verb-anchored permission rules and push parser; overlaps later Rust bash-guard but the fence test corpus is independent. | autoos:T4-MSG, routing:GATE-P1 | dep T4-HOST (cancelled) edge must drop; overlap SU-HOOK-BASHGUARD |
| autoos:T4-HARDEN | INDEPENDENT |  | Container init/pids/mem hardening and opencode update job; ops, valid while the fleet container runs. | autoos:T4-HOST |  |
| autoos:T4-HOST | CANCEL | design section 2 (what disappears), section 10 deletions; Temporal | Host systemd heartbeat/auto-resume of orchestrators: persistent orchestrators, heartbeat crons and fleet-revive units are removed. | autoos:T4-FENCE, routing:GATE-P1 | re-point T4-FENCE and routing:GATE-P1 |
| autoos:T4-MSG | CANCEL | design section 6 | Inter-agent order/ack/reply messaging replaced by state, keeper events and Inbox; no worker-to-worker talk. | autoos:T4-ZEN | re-point T4-ZEN |
| autoos:T4-ZEN | INDEPENDENT |  | opencode config ships Zen provider with own key; Zen free leg stays, but near-duplicate of T1-ZEN. | routing:T4-CERT | near-duplicate of T1-ZEN; dep T4-MSG cancelled, drop edge |
| autoos:T5-BRANCHES | INDEPENDENT |  | Merge/drop branch candidates table; one-off repo cleanup. | routing:GATE-P2 | table goes to L0 inbox (moot target) |
| autoos:T5-CERT | INDEPENDENT |  | opencode certification scorecard on workstation; harness evidence valid for D17. | routing:GATE-P3 |  |
| autoos:T5-CLONE | INDEPENDENT |  | Agent work in dedicated fleet clones; hygiene. | routing:GATE-P2 |  |
| autoos:T5-FINISH | INDEPENDENT |  | Let running opencode sessions finish in-flight items; one-off drain. | routing:GATE-P2 | unsure: fleet slimming (D5) may make it moot |
| autoos:T5-GRAPH | INDEPENDENT |  | Workstation uses central omnigraph; omnigraph stays (Knowledge tab). | routing:GATE-P2 |  |
| autoos:T5-GROK-L2 | INDEPENDENT |  | Grok bot behind Tailscale Funnel smoke; independent product work, though D21 forbids Funnel for OmniRoute. | autoos:GROK-UPSTREAM-PR, routing:GATE-P2 | unsure: D21 no Funnel applies to OmniRoute only |
| autoos:T5-HANDOFF | INDEPENDENT |  | One current workstation handoff doc pointing to plan v3 lanes; doc hygiene. | routing:GATE-P2 | unsure: plan v3 lanes partly superseded |
| autoos:T5-KEYS | INDEPENDENT |  | Workstation key file for server/workstation gateways; ops. | autoos:T1-PEER, routing:GATE-P2 |  |
| autoos:T5-PROVIDERS | INDEPENDENT |  | Verify vertex/ovh credit consumption; provider ops feeding trial-credit legs. | routing:GATE-P2 |  |
| autoos:T5-SKILLS | INDEPENDENT |  | Review of combo-create and unattended-orchestration skills; unattended-orchestration is rewritten in P4. | routing:GATE-P2 | unsure: review of old unattended-orchestration wasted; combo-create review stays |
| autoos:T5-SYNC | INDEPENDENT |  | Workstation OmniRoute config from same registry with daily drift; extends drift check of RT-P0-OR-DRIFT. | routing:GATE-P2 | overlap RT-P0-OR-DRIFT |
| autoos:T7-CONSOLE | CANCEL | design section 7, D16; plangraph:RT-P3-SVELTE, plangraph:UI-FLEET-TAB | Separate fleet console replaced by the one SvelteKit plangraph page with tabs; fleet view is UI-FLEET-TAB. | routing:GATE-P3 | FLEETSPEC acceptance tests not carried over; dep plangraph:PG-M4 unaffected |
| autoos:T7-CONSOLE-MSG | CANCEL | design section 6, 7; plangraph:RT-P3-TABS | Console messaging pages: messaging dropped, console replaced by plangraph tabs; a secret-audit page would be a new tab item. | routing:GATE-P3 | XSECRET audit view needs a new item if wanted |
| autoos:T7-GRAPH-A | INDEPENDENT |  | AutoOS omnigraph content (components, ADRs, tasks); omnigraph feeds planners and the Knowledge tab. | routing:GATE-P3 |  |
| autoos:T7-GRAPH-USE | INDEPENDENT |  | Agents use graph context_pack at session start; overlaps planner kit context pack and relaunch metrics of persistent sessions. | routing:GATE-P3 | unsure: overlap RT-P1-PLANNER-KIT context pack |
| autoos:TIER-CONTRACT | INDEPENDENT |  | Reframed t1 tier routing rule; keeper-side routing by risk/estimate may subsume it, decision pending in routing:Q-TIER-CONTRACT. | routing:PH-AUDIT-1002 | unsure: tier concept vs window class/leg tier; deps T2-COMPACT, Q-TIER-CONTRACT |
| autoos:U12 | INDEPENDENT |  | OmniRoute translator fix for vertex 400; gateway bug fix. | routing:PH-FLEET-1003 |  |
| autoos:WORKST-L0-FAILOVER | CANCEL | D9; design section 2 | Persistent workstation L0 failover: no persistent L0, Temporal owns execution and workers pull; nothing to fail over. | routing:PH-FLEET-1003 |  |
| autoos:WRITER-BENCH | INDEPENDENT |  | Writer benchmark across legs; evidence for leg order and A/B, unrelated to removal. | autoos:PH-OPS-20261002 |  |
| autoos:WS-1 | INDEPENDENT |  | One-off PAIDSTOP move to workstation; many live dependents, rethink does not clearly obsolete it. | autoos:MCP-REMOTE, autoos:MCP-SEAT-EVIDENCE, autoos:PH-WS-SHIFT, autoos:SKILL2MCP, autoos:T0-PAID, autoos:T2-MCPLIFE, autoos:WS-6 | unsure: interim fleet migration |
| autoos:WS-2 | CANCEL | design section 4 (card + Temporal activity), D11 | Taildrop run-request contract and receipt-by-sha replaced by cards, worker activities on the workstation, and events. | autoos:PH-WS-SHIFT, autoos:WS-6 | drop edges on PH-WS-SHIFT, WS-6 |
| autoos:WS-3 | INDEPENDENT |  | CI-like harness on workstation (WSL2/Docker); still valuable for worker checks and local-green. | autoos:PH-WS-SHIFT, autoos:WS-6 | unsure: may fold into worker checks (RT-P1-WORKER) |
| autoos:WS-4 | COVERED | autoos:SPAWN-USES-PLACEMENT | Folded into placement API by orch-setup S22 (WorkstationUsableGuard, 80 GB budget). | autoos:PH-WS-SHIFT, autoos:WS-6 | re-point dependents to SPAWN-USES-PLACEMENT |
| autoos:WS-5 | CANCEL | D9, D5 | New L2 sessions on the workstation via workst-L1: no persistent L1/L2 sessions in the target (D9). | autoos:PH-WS-SHIFT, autoos:WS-6 | drop edges on PH-WS-SHIFT, WS-6 |
| autoos:WS-6 | INDEPENDENT |  | Slot keeper and 0 L3 runs on coding.vm; slot part overlaps placement but done criterion is operational and D-437 kept. | autoos:MEMGUARD, autoos:PH-WS-SHIFT | unsure: overlap SPAWN-USES-PLACEMENT; deps WS-2, WS-4, WS-5 resolved |
| autoos:ZCODE | INDEPENDENT |  | Z.ai coding installer, compose, docs; ops lane. | autoos:PH-OPS-20261002, autoos:T4-HARDEN, autoos:ZCODE-GLM-BURN |  |
| autoos:ZCODE-CLIENT-LANE-Z | INDEPENDENT |  | ZCode spawn client on app-server protocol; spawner substrate item, but zcode is not in the D17 harness list. | routing:PH-FLEET-1003 | unsure: zcode not a D17 harness; freeze D20 |
| autoos:ZCODE-GLM-BURN | INDEPENDENT |  | Time-critical GLM grant burn; grant expired about 2026-10-07 so likely moot, but not design-driven. | routing:PH-AUDIT-1002 | unsure: deadline passed 2026-10-07; owner to close if grant lapsed |
| plangraph:API-OIDC | INDEPENDENT | - | GUI write auth still needed; design leaves the login path to build time (sec 7); may merge into SvelteKit/RT-P3-PHONE auth. | plangraph:PH-M2C-API, plangraph:UI-WRITES | unsure: auth design may be redone under RT-P3-PHONE/SvelteKit |
| plangraph:API-PUSH | COVERED | plangraph:Q-API-PUSH | Operator ruling S19: folded into new Q-API-PUSH; PH-M2C-API re-pointed in the orch-setup batch. | plangraph:PH-M2C-API (re-point to Q-API-PUSH) | - |
| plangraph:CI-RUNS-ON-WORKST | INDEPENDENT | - | Re-scoped by orch-setup S18/S19: private repos on the workstation runner, public repo GitHub-hosted only. | routing:RT-P1 (already an RT-P1 dep) | re-scoped |
| plangraph:KG-ADAPTER | INDEPENDENT | - | Knowledge-graph backend evaluation (omnigraph vs Graphiti); design keeps omnigraph as Knowledge store but does not cover this evaluation. | plangraph:KG-VIEW | unsure: Graphiti evaluation not in design |
| plangraph:KG-OMNIGRAPH-REMOVE | INDEPENDENT | - | Design D16/sec 7 embeds the omnigraph viewer as the Knowledge tab; removing the omnigraph UI conflicts with it. | plangraph:PH-M2C-KG | unsure: conflicts with D16; candidate cancel, operator confirm |
| plangraph:KG-VIEW | INDEPENDENT | - | KG view inside plangraph overlaps the RT-P3-TABS Knowledge tab (omnigraph embedded); depends on static UI-SHELL. | plangraph:KG-OMNIGRAPH-REMOVE | unsure: overlaps RT-P3-TABS |
| plangraph:M2B-OFFHOST | INDEPENDENT | - | Keeper on cloud.vm conflicts with D11/D12 (keeper on coding.vm Postgres now) and overlaps server:RT-P6-MOVE (P6). | plangraph:PG-M2B | unsure: candidate cancel in favour of RT-P6-MOVE |
| plangraph:MEM-OMNIGRAPH-MERGE | INDEPENDENT | - | Omnigraph memory/server improvement; omnigraph stays the knowledge store (sec 2). Its retire-viewer step conflicts with the D16 Knowledge tab. | plangraph:PG-OMNIGRAPH-UI | check viewer-retire step against D16 |
| plangraph:MODEL-HOST | INDEPENDENT | - | Data-model chore (host as attribute); compatible with orch-setup host-agent/placement (S20-S22), not replaced by it. | plangraph:PH-M2C-MODEL | related to HOST-SAMPLES-API |
| plangraph:MODEL-LOOP | INDEPENDENT | - | Plan-graph data-model feature unrelated to the rethink phases. | plangraph:PH-M2C-MODEL | - |
| plangraph:MODEL-NAMES | INDEPENDENT | - | Plan-graph readable titles/descriptions; unrelated to the rethink. | plangraph:PH-M2C-MODEL | - |
| plangraph:MODEL-STATS | INDEPENDENT | - | Per-task cost/time/review stats partly overlap RT-P1-GOVERNOR metrics and the attempts projection (sec 3, 9), but scope differs. | plangraph:PH-M2C-MODEL | unsure: overlap with RT-P1-GOVERNOR |
| plangraph:OP-PUBLIC-LOOK | INDEPENDENT | - | SPLIT track kept: orch-setup S2 makes the public repo a fresh repo from make_public.py via SPLIT-PUBLISH. | plangraph:SPLIT-PUBLISH | operator gate |
| plangraph:PG-ANSWER-EDGE-LIVE | INDEPENDENT | - | Edge Authelia path for answers; design sec 7 reuses the existing edge/Authelia login. Needs its own GO. | plangraph:PH-QA-FLOW | possible overlap with QA-GATE |
| plangraph:PG-ANSWER-UI | INDEPENDENT | - | Operator answer verb and Q&A write mode; feeds PG-INBOX, which Q-API-PUSH depends on. Overlaps RT-P1-INBOX but the keeper API is still needed. | plangraph:PG-ANSWER-EDGE-LIVE, PG-INBOX, PH-QA-FLOW | overlap RT-P1-INBOX |
| plangraph:PG-ARCHIVE-OPKIND-LOW | CANCEL | plangraph:RT-P4-LEDGER-CUTOVER | LOW defect in the md_sync_archive view; md-sync code and markdown ledgers are archived/deleted by RT-P4-LEDGER-CUTOVER (design sec 10, P4). | plangraph:PH-QA-FLOW (drop edge) | no keeper data affected |
| plangraph:PG-CI-FORGEJO | INDEPENDENT | - | Keeper CI verification adapter for Forgejo projects; unrelated to the rethink. | plangraph:PH-M2C-API | related KEEPER-CI-* items |
| plangraph:PG-DEC-ATTRIBUTION | INDEPENDENT | - | Design sec 10 keeps the PG-DEC-* chain (decisions table); D14 narrows decisions to operator rows, so scope may shrink. | plangraph:PG-DEC-EDIT-NOTICE, PH-QA-FLOW | kept per design; consider joining RT-P2-DECISIONS |
| plangraph:PG-DEC-COUNT | INDEPENDENT | - | Live bug in the md-sync decision count; moot only after RT-P4-LEDGER-CUTOVER, still valid now (md-sync stop-gap D-1297). | routing:PH-OPPLAN-1012 | - |
| plangraph:PG-DEC-EDIT-NOTICE | INDEPENDENT | - | Design sec 10 keeps the PG-DEC-* chain; UI edit of decisions on the decisions table. | plangraph:PH-QA-FLOW | kept per design; consider joining RT-P2-DECISIONS |
| plangraph:PG-ESTIMATE-SRC-EDIT | INDEPENDENT | - | Keeper audit-meta bug for estimate_src; unrelated to the rethink. | plangraph:PH-QA-FLOW | - |
| plangraph:PG-INBOX | INDEPENDENT | - | Orch-setup keeps it: Q-API-PUSH depends on PG-INBOX (inbox long-poll for planner wake). Overlaps RT-P1-INBOX/RT-P3-PHONE wake. | plangraph:PG-UI-BRIEF, PH-QA-FLOW (+Q-API-PUSH) | named overlap; keep |
| plangraph:PG-L1-CREATE-WITH-EDGE | INDEPENDENT | - | Keeper verb so a creator attaches an item under a gate atomically; still useful for planner batches. | routing:LESSONS-ENFORCE | - |
| plangraph:PG-L2-SMALLFIX-RULE | INDEPENDENT | - | Rule text for L1/L2 brief templates; L1/L2 sessions are replaced (D9) but the fleet runs interim and the rule is an operator order. | routing:PH-OPPLAN-1012 | unsure: candidate cancel after RT-P4-SKILLS-REWRITE |
| plangraph:PG-LE-DEPS-OPS | INDEPENDENT | - | Keeper add_dep/remove_dep batch ops; useful for planner-kit batches, unrelated to replaced parts. | routing:LESSONS-ENFORCE | - |
| plangraph:PG-LE-DROW-LINT | INDEPENDENT | - | Lint for markdown D-rows; moot once RT-P2-DECISIONS stops markdown writes, but md-sync stop-gap runs until then. | routing:LESSONS-ENFORCE | unsure: candidate cancel after RT-P2-DECISIONS |
| plangraph:PG-LE-GUARD-KEY | INDEPENDENT | - | Keeper credential-guard message/false-positive fix; unrelated to the rethink. | routing:LESSONS-ENFORCE | - |
| plangraph:PG-M2B | INDEPENDENT | - | Hardening umbrella (run-bound tokens, signed snapshot, off-host keeper) partly overlaps SU-RUN-TOKENS, RT-P1-KEEPER-PG, RT-P6-MOVE. | plangraph:API-OIDC, PG-M3 | unsure: SQLite-era subitems may be superseded by the Postgres keeper |
| plangraph:PG-M3 | INDEPENDENT | - | Scorecard/attack/failure-drill tests; depends on PG-M2B, whose scope is unsure after the Postgres keeper move. | - | unsure: tied to PG-M2B |
| plangraph:PG-MERGE-ROUTING-PLAN | CANCEL | routing:RT-P4 (RT-P4-ROUTER-CODE, RT-P4-LEDGER-CUTOVER, RT-P4-ARCHIVE) | Plan-only routing-into-plangraph migration is superseded by design sec 10 and the P4 items, which already plan the move. | routing:PH-OPENWORK-1013 (re-point to routing:RT-P4) | D-1277 lineage, operator order: confirm at merge |
| plangraph:PG-NEEDS-YOU | INDEPENDENT | - | Needs-you page overlaps the RT-P3-TABS Inbox tab; commit a01c8ed already built a Needs You page on the static UI. | plangraph:PG-QUESTION-CARDS, routing:PH-OPPLAN-1012 | unsure: likely already done; overlaps RT-P3-TABS |
| plangraph:PG-OMNIGRAPH-UI | INDEPENDENT | - | Gate whose open deps (MEM-OMNIGRAPH-MERGE, KG items) are independent. | plangraph:PH-M2C-UI | - |
| plangraph:PG-PROJECT-PRIORITY | INDEPENDENT | - | Project priority order in the plangraph setup; unrelated to the replaced parts. | plangraph:PH-M2C-UI | - |
| plangraph:PG-PROXY-ACCESS | INDEPENDENT | - | Design sec 12 reuses the Caddy edge + mTLS keeper path; item stays. ROUTE-PG-SSOT is re-pointed to SU-MCP by orch-setup. | plangraph:M2B-OFFHOST, routing:ROUTE-PG-SSOT | named overlap with ROUTE-PG-SSOT |
| plangraph:PG-PUBLIC-READY | COVERED | routing:RT-P4 | Kept by design sec 10; already a dep of RT-P4-SPAWNER and RT-P4-PUBLIC-FLIP, so under the P4 gate. | plangraph:RT-P4-PUBLIC-FLIP, RT-P4-SPAWNER, routing:PH-OPPLAN-1012 | - |
| plangraph:PG-QUESTION-CARDS | INDEPENDENT | - | Question cards UI overlaps the RT-P3-TABS Inbox and Q-API-PUSH interactive questions; still valid on the interim page. | routing:PH-OPPLAN-1012 | unsure: overlaps Inbox tab; duplicate of QA-UI? |
| plangraph:PG-ROUTER-REOPEN | INDEPENDENT | - | Keeper verb to reopen in_review lanes; unrelated to the replaced parts. | plangraph:PH-M2C-API | - |
| plangraph:PG-UI-BRIEF | INDEPENDENT | - | UI detail panel with a generated brief; related to planner kit/context pack but a different deliverable. | plangraph:PH-QA-FLOW | related RT-P1-PLANNER-KIT |
| plangraph:PG-UI-TABICON | INDEPENDENT | - | Small UI favicon item; unrelated to the rethink, carried through the SvelteKit conversion. | plangraph:PH-M2C-UI | - |
| plangraph:PH-M2C | INDEPENDENT | - | Umbrella gate with many open independent deps. | plangraph:RUST-PORT (drop edge, cancelled) | - |
| plangraph:PH-M2C-API | INDEPENDENT | - | Gate: only API-PUSH is covered; other deps are independent. | plangraph:PH-M2C | re-point dep API-PUSH to Q-API-PUSH |
| plangraph:PH-M2C-KG | INDEPENDENT | - | Gate: deps KG-OMNIGRAPH-REMOVE and PG-KG-013-HEADER are independent. | plangraph:PH-M2C | - |
| plangraph:PH-M2C-MODEL | INDEPENDENT | - | Gate: all MODEL deps are independent. | plangraph:PH-M2C | - |
| plangraph:PH-M2C-SPLIT | INDEPENDENT | - | Gate: SPLIT-PUBLISH is kept by orch-setup S2. | plangraph:PH-M2C | - |
| plangraph:PH-M2C-SYNC | INDEPENDENT | - | Gate: md-sync deps; md-sync is deleted at RT-P4-LEDGER-CUTOVER, but open deps are independent. | plangraph:PH-M2C | unsure: md-sync end-of-life |
| plangraph:PH-M2C-UI | INDEPENDENT | - | Gate: many open UI deps independent; QA-ROUTER is cancelled, drop that edge. | plangraph:PH-M2C | drop dep QA-ROUTER |
| plangraph:PH-QA-FLOW | INDEPENDENT | - | Gate: deps mostly independent; PG-ARCHIVE-OPKIND-LOW is cancelled, drop that edge. | plangraph:PH-M2C | drop dep PG-ARCHIVE-OPKIND-LOW |
| plangraph:QA-GATE | INDEPENDENT | - | Answer gate files for nginx/Caddy; may duplicate PG-ANSWER-EDGE-LIVE; go-live parked on the mTLS hop. | plangraph:PH-M2C-UI | unsure: possible duplicate of PG-ANSWER-EDGE-LIVE |
| plangraph:QA-ROUTER | CANCEL | plangraph:PG-INBOX; design sec 2, sec 6 | Router heartbeat forwarding to owner L1 and D-row mirroring: router, L1s and heartbeat crons disappear; delivery is PG-INBOX/RT-P1-INBOX. | plangraph:PH-M2C-UI (drop edge) | dep QA-VERBS stays independent |
| plangraph:QA-UI | INDEPENDENT | - | Needs You choice cards (older D-865 design); likely duplicate of PG-QUESTION-CARDS/PG-ANSWER-UI but carries security tests. | plangraph:PH-M2C-UI, QA-GATE | unsure: duplicate of PG-QUESTION-CARDS? |
| plangraph:QA-VERBS | INDEPENDENT | - | Operator role/answer verbs (D-865); likely duplicate of PG-ANSWER-UI (D-1012) but not confirmed. | plangraph:PH-M2C-UI, QA-ROUTER, QA-UI | unsure: duplicate of PG-ANSWER-UI? |
| plangraph:RT-P1-E2E | COVERED | routing:RT-P1 | RT item already a dep of routing:RT-P1 (rt_items.json); no change. | see rt_items.json | - |
| plangraph:RT-P1-GOVERNOR | COVERED | routing:RT-P1 | RT item already a dep of routing:RT-P1 (rt_items.json); no change. | see rt_items.json | - |
| plangraph:RT-P1-INBOX | COVERED | routing:RT-P1 | RT item already a dep of routing:RT-P1 (rt_items.json); no change. | see rt_items.json | - |
| plangraph:RT-P1-PROJECTOR | COVERED | routing:RT-P1 | RT item already a dep of routing:RT-P1 (rt_items.json); no change. | see rt_items.json | - |
| plangraph:RT-P1-REVIEW | COVERED | routing:RT-P1 | RT item already a dep of routing:RT-P1 (rt_items.json); no change. | see rt_items.json | - |
| plangraph:RT-P1-WORKER | COVERED | routing:RT-P1 | RT item already a dep of routing:RT-P1 (rt_items.json); no change. | see rt_items.json | - |
| plangraph:RT-P2-AGY | COVERED | routing:RT-P2 | RT item already a dep of routing:RT-P2 (rt_items.json); no change. | see rt_items.json | - |
| plangraph:RT-P2-DECISIONS | COVERED | routing:RT-P2 | RT item already a dep of routing:RT-P2 (rt_items.json); no change. | see rt_items.json | PG-DEC-* chain may join |
| plangraph:RT-P2-LEGORDER | COVERED | routing:RT-P2 | RT item already a dep of routing:RT-P2 (rt_items.json); no change. | see rt_items.json | - |
| plangraph:RT-P2-QODER | COVERED | routing:RT-P2 | RT item already a dep of routing:RT-P2 (rt_items.json); no change. | see rt_items.json | - |
| plangraph:RT-P3-PHONE | COVERED | routing:RT-P3 | RT item already a dep of routing:RT-P3 (rt_items.json); no change. | see rt_items.json | Q-API-PUSH depends on it |
| plangraph:RT-P3-SVELTE | COVERED | routing:RT-P3 | RT item already a dep of routing:RT-P3 (rt_items.json); no change. | see rt_items.json | - |
| plangraph:RT-P3-TABS | COVERED | routing:RT-P3 | RT item already a dep of routing:RT-P3 (rt_items.json); no change. | see rt_items.json | - |
| plangraph:RT-P4-LEDGER-CUTOVER | COVERED | routing:RT-P4 | RT item already a dep of routing:RT-P4 (rt_items.json); no change. | see rt_items.json | - |
| plangraph:RT-P4-PUBLIC-FLIP | COVERED | routing:RT-P4 | RT item already a dep of routing:RT-P4 (rt_items.json); no change. | see rt_items.json | overlap: orch-setup S2/11.1 makes the public repo a fresh one via SPLIT-PUBLISH; flip semantics differ |
| plangraph:RT-P4-ROUTER-CODE | COVERED | routing:RT-P4 | RT item already a dep of routing:RT-P4 (rt_items.json); no change. | see rt_items.json | - |
| plangraph:RT-P4-SKILLS-REWRITE | COVERED | routing:RT-P4 | RT item already a dep of routing:RT-P4 (rt_items.json); no change. | see rt_items.json | absorbs autoos:T2-SKILL (cancelled) |
| plangraph:RT-P4-SPAWNER | COVERED | routing:RT-P4 | RT item already a dep of routing:RT-P4 (rt_items.json); no change. | see rt_items.json | - |
| plangraph:RT-P5-JEV | COVERED | routing:RT-P5 | RT item already a dep of routing:RT-P5 (rt_items.json); no change. | see rt_items.json | named overlap T1-JEV/ROUTE-JEV-TRIAL (other chunks) |
| plangraph:RT-P5-OPENHANDS | COVERED | routing:RT-P5 | RT item already a dep of routing:RT-P5 (rt_items.json); no change. | see rt_items.json | - |
| plangraph:RT-P5-OR-MCP | COVERED | routing:RT-P5 | RT item already a dep of routing:RT-P5 (rt_items.json); no change. | see rt_items.json | - |
| plangraph:RT-P5-PLUGINS | COVERED | routing:RT-P5 | RT item already a dep of routing:RT-P5 (rt_items.json); no change. | see rt_items.json | - |
| plangraph:RT-P5-UNATTENDED-PLAN | COVERED | routing:RT-P5 | RT item already a dep of routing:RT-P5 (rt_items.json); no change. | see rt_items.json | - |
| plangraph:RT-P6-RUST-KEEPER | COVERED | routing:RT-P6 | RT item already a dep of routing:RT-P6 (rt_items.json); no change. | see rt_items.json | - |
| plangraph:RUST-PORT | CANCEL | plangraph:RT-P3-SVELTE, plangraph:RT-P6-RUST-KEEPER | Scope-less placeholder; web page goes to SvelteKit (D24) and keeper to Rust in P6 (D12), both already planned. | routing:PH-AUDIT-1002 (drop edge) | deps PG-M4, PH-M2C |
| plangraph:SPLIT-PUBLIC-HISTORY | INDEPENDENT | - | SPLIT track kept: orch-setup S2 builds the fresh public repo via make_public.py; SU-RELEASE depends on SPLIT-PUBLISH. | plangraph:OP-PUBLIC-LOOK | - |
| plangraph:SPLIT-PUBLISH | INDEPENDENT | - | Kept by orch-setup S2; SU-RELEASE depends on it. | plangraph:PH-M2C-SPLIT (+SU-RELEASE) | - |
| plangraph:UI-TABLE | INDEPENDENT | - | Table tab on the static page; SvelteKit conversion (RT-P3-SVELTE) ports it. Commit a01c8ed already added a groupable table page. | plangraph:PH-M2C-UI, UI-WRITES | unsure: possibly already done |
| plangraph:UI-WRITES | INDEPENDENT | - | GUI write flows overlap RT-P3-PLAN-MODE and RT-P3-SVELTE; still needed in some form. | plangraph:PH-M2C-UI | unsure: overlaps RT-P3-PLAN-MODE |
| routing:ADOPT-400LOC-ROUTING | INDEPENDENT | - | 400-line guard in the routing CI; still valid until routing is archived. | r:ROUTE-400LOC | unsure: low value, routing archived at RT-P4-ARCHIVE; cancel candidate |
| routing:COORD-DIET-PILOT | CANCEL | design s6 / RT-P4-SKILLS-REWRITE | Design s6 replaces messaging: status is state, only question/decision/done/alarm; the pilot measures a relay model that disappears. | r:PH-OPPLAN-1012 | named overlap; drop edge from PH-OPPLAN-1012 |
| routing:CTX-WATCHDOG-FRESH | INDEPENDENT | - | Finished lane (32 tests) fixing the interim watchdog; persistent L1s still exist until P2. | r:PH-OPPLAN-1012 | unsure: overlaps autoos:AO-CTX-RECYCLE-ALLHOSTS (COVERED by SU-HOOK-CTXGUARD, first_ts/ready --card); merge, then retire via RT-P4-ROUTER-CODE; edge to PH-OPPLAN-1012 |
| routing:DECISION-MODELS-RESEARCH | CANCEL | design D18 / routing:RT-P5-JEV | Question already decided: rules decide, free Jev shadow evaluated in RT-P5-JEV; routing is keeper-side code (s2, s4.3). | r:PH-OPPLAN-1012 | fold any existing findings into RT-P5-JEV; drop edge from PH-OPPLAN-1012 |
| routing:GATE-P0 | INDEPENDENT | - | Plan v3 phase gate over autoos T0 items; outcome depends on other chunks. | autoos:T1-COMBO3, autoos:T1-CTX, autoos:T2-ONEPATH, autoos:T2-RECORD, autoos:T4-FENCE, autoos:T4-HOST | cancel if all deps get cancelled/covered in the merge |
| routing:GATE-P1 | INDEPENDENT | - | Plan v3 phase gate (main green, combos v3 drift 0); drift part overlaps autoos:RT-P0-OR-DRIFT. | autoos:T1-LITELLM, autoos:T1-PEER, autoos:T1-PROV, autoos:T2-RULES (+13 more) | cancel if all deps get cancelled/covered; dependent autoos:T2-SKILL is cancelled (S19) |
| routing:GATE-P2 | INDEPENDENT | - | Plan v3 gate; still has live deps. T2-SKILL edge is dropped by S19. | autoos:T7-CONSOLE, autoos:T7-CONSOLE-MSG, autoos:T7-GRAPH-A, autoos:T7-GRAPH-USE, r:T4-CERT, r:T4-SWITCH, r:T7-GRAPH-R | S19 drops the T2-SKILL edge; T1-LITELLM/T5-* decided in autoos chunk; cancel if all deps cancelled |
| routing:GATE-P3 | INDEPENDENT | - | Plan v3 gate; deps include T4-CERT/T4-SWITCH (cancelled here) and autoos T7/T5 items. | r:GOAL | cancel if all remaining deps are cancelled; GOAL depends on it |
| routing:GOAL | INDEPENDENT | - | Top goal 'Plan v3 complete'; PH-RETHINK reaches it only via SETUP-IMPROVEMENT-PLAN. | - | operator confirm: retitle to the D-1285 goal (design s0); if SETUP-IMPROVEMENT-PLAN is cancelled, re-point PH-RETHINK directly to GOAL |
| routing:HAIKU-PROPAGATE | INDEPENDENT | - | Haiku-for-read-only-checks rule still serves the interim fleet, which persists until P2. | r:PH-OPPLAN-1012, r:SETUP-IMPROVEMENT-PLAN | unsure: moot once L1s are gone (D9); design s8 covers token hygiene; deps autoos:AO-HAIKU |
| routing:JULES-PR-TRIAGE | INDEPENDENT | - | PR hygiene across gen-analysis, Tools, Ventu; product repos, not orchestration. | r:PH-OPPLAN-1012 | - |
| routing:LESSONS-ENFORCE | INDEPENDENT | - | Epic gate with many open deps; keeper/MCP enforcement still valid. | r:GOAL | dep autoos:AO-SLOT-GRANT-TOKEN is COVERED (re-point to autoos:SPAWN-USES-PLACEMENT, S22); dependent GOAL |
| routing:OS-102B | INDEPENDENT | - | operator or server ops step, unrelated to the rethink design | r:PH-SERVER-1003 | - |
| routing:OS-106 | INDEPENDENT | - | operator or server ops step, unrelated to the rethink design | r:PH-OPERATOR | - |
| routing:OS-111 | INDEPENDENT | - | operator or server ops step, unrelated to the rethink design | r:PH-OPERATOR | - |
| routing:OS-115 | INDEPENDENT | - | operator or server ops step, unrelated to the rethink design | r:PH-OPERATOR | - |
| routing:OS-117 | INDEPENDENT | - | operator or server ops step, unrelated to the rethink design | r:PH-OPERATOR | - |
| routing:OS-124B | INDEPENDENT | - | operator or server ops step, unrelated to the rethink design | r:PH-OPERATOR | - |
| routing:OS-132 | INDEPENDENT | - | operator or server ops step, unrelated to the rethink design | r:PH-OPERATOR | - |
| routing:OS-133 | INDEPENDENT | - | operator or server ops step, unrelated to the rethink design | r:PH-OPERATOR | - |
| routing:OS-135 | INDEPENDENT | - | Router model Sonnet 5.5 part is done; subscription reporting still real (D7). | r:PH-OPERATOR | unsure: 'router model' moot with no persistent router (D9) |
| routing:OS-138 | INDEPENDENT | - | operator or server ops step, unrelated to the rethink design | r:PH-OPERATOR | - |
| routing:OS-140 | INDEPENDENT | - | Launch two ebn L2 sessions; ebn science product work. | r:PH-OPERATOR | peer claim, may be stale; ebn L1 withdrew the lines |
| routing:OS-141 | INDEPENDENT | - | Revive prox-L1-main from its handoff; interim fleet, server work. | r:PH-OPERATOR | unsure: persistent L1 recycle |
| routing:OS-144 | INDEPENDENT | - | operator or server ops step, unrelated to the rethink design | r:PH-OPERATOR | - |
| routing:OS-145 | INDEPENDENT | - | operator or server ops step, unrelated to the rethink design | r:PH-OPERATOR | - |
| routing:OS-148 | INDEPENDENT | - | Standing delegation text for routing-00 on all hosts; routing-00 persists until P4. | r:PH-OPERATOR | unsure: routing-00 is the persistent router that D9 removes; keep during build |
| routing:OS-149 | INDEPENDENT | - | Anchor log for the SQLite keeper.db; keeper moves to Postgres in RT-P1-KEEPER-PG. | r:PH-OPERATOR | unsure: once on, restoring an older keeper.db is refused, which collides with the blue-green 48 h read-only cutover; operator decide |
| routing:OS-151 | INDEPENDENT | - | Workstation cannot reach keeper; PG-PROXY chain exists. P1.1 needs the same network path for the WSL2 worker. | r:PH-OPERATOR | unsure: likely stale after the keeper proxy (OS-160); operator confirm close |
| routing:OS-152 | INDEPENDENT | - | operator or server ops step, unrelated to the rethink design | r:PH-OPERATOR | - |
| routing:OS-153 | INDEPENDENT | - | Restart workstation L1s from handoffs; interim fleet still runs (D5 slims, does not stop it). | r:PH-OPERATOR | unsure: may be moot if the operator orders the fleet down |
| routing:OS-158 | INDEPENDENT | - | operator or server ops step, unrelated to the rethink design | r:PH-OPERATOR | - |
| routing:OS-160 | INDEPENDENT | - | Outside probe of the keeper hostname; the Caddy edge + mTLS path is reused by the rethink (s12). | r:PH-OPERATOR | - |
| routing:OS-163 | INDEPENDENT | - | operator or server ops step, unrelated to the rethink design | r:PH-OPERATOR | - |
| routing:OS-166 | INDEPENDENT | - | operator or server ops step, unrelated to the rethink design | r:PH-OPERATOR | - |
| routing:OS-171 | INDEPENDENT | - | operator or server ops step, unrelated to the rethink design | r:PH-OPERATOR | - |
| routing:OS-173 | INDEPENDENT | - | operator or server ops step, unrelated to the rethink design | r:PH-OPERATOR | - |
| routing:OS-174 | INDEPENDENT | - | operator or server ops step, unrelated to the rethink design | r:PH-OPERATOR | - |
| routing:OS-176 | INDEPENDENT | - | operator or server ops step, unrelated to the rethink design | r:PH-OPERATOR | - |
| routing:OS-178 | INDEPENDENT | - | operator or server ops step, unrelated to the rethink design | r:PH-OPERATOR | - |
| routing:OS-179 | INDEPENDENT | - | operator or server ops step, unrelated to the rethink design | r:PH-OPERATOR | - |
| routing:OS-183 | INDEPENDENT | - | Re-auth the muse-spark leg or drop it; S17 uses opencode/muse-spark-1.3-free as a seat. | r:PH-OPERATOR | relevant to leg order (design s5) |
| routing:OS-184 | INDEPENDENT | - | operator or server ops step, unrelated to the rethink design | r:PH-OPERATOR | - |
| routing:OS-185 | INDEPENDENT | - | operator or server ops step, unrelated to the rethink design | r:PH-OPERATOR | - |
| routing:OS-186 | INDEPENDENT | - | operator or server ops step, unrelated to the rethink design | r:PH-OPERATOR, server:SEMAPHORE-KEY-SCOPE | - |
| routing:OS-187 | INDEPENDENT | - | gen-analysis config cleanup; gen product work. | r:PH-OPERATOR | answered by OS-205 (a); operator confirm close |
| routing:OS-188 | INDEPENDENT | - | Question about gen L2 runner config; gen product work. | r:PH-OPERATOR | answered by OS-205 (D-1507); operator confirm close |
| routing:OS-189 | INDEPENDENT | - | operator or server ops step, unrelated to the rethink design | r:PH-OPERATOR | - |
| routing:OS-190 | INDEPENDENT | - | operator or server ops step, unrelated to the rethink design | r:PH-OPERATOR | - |
| routing:OS-191 | INDEPENDENT | - | Off-host backup question for the Postgres keeper (RT-P1-PG-DEPLOY). | r:PH-OPERATOR | unsure: appears superseded by OS-191B / D-1375 (VM+NFS backup covers it); operator confirm duplicate |
| routing:OS-191B | INDEPENDENT | - | Not blocking (D-1375); optional B2 off-host backup for the Postgres keeper; design s12 risk row mentions off-host backup. | r:PH-OPERATOR | feeds server:RT-P1-PG-DEPLOY only softly |
| routing:OS-192 | INDEPENDENT | - | operator or server ops step, unrelated to the rethink design | r:PH-OPERATOR | - |
| routing:OS-193 | INDEPENDENT | - | Start workstation runner session for waiting server jobs; interim fleet work. | r:PH-OPERATOR | unsure: persistent session, server jobs depend on it |
| routing:OS-194 | INDEPENDENT | - | operator or server ops step, unrelated to the rethink design | r:PH-OPERATOR | - |
| routing:OS-195 | INDEPENDENT | - | Incident about a stale OmniRoute data dir; related to autoos:RT-P0-OR-DRIFT but an operator ruling. | r:PH-OPERATOR | unsure: title truncated; relation to drift lane not verified |
| routing:OS-196 | INDEPENDENT | - | Operator step to start the L2 session that builds RT-P1-WORKER-CODE; P1 critical path needs it. | r:PH-OPERATOR | close candidate: its target plangraph:RT-P1-WORKER-CODE is done (L1 review) |
| routing:OS-197 | INDEPENDENT | - | operator or server ops step, unrelated to the rethink design | r:PH-OPERATOR | - |
| routing:OS-198 | INDEPENDENT | - | operator or server ops step, unrelated to the rethink design | r:PH-OPERATOR | - |
| routing:OS-199 | INDEPENDENT | - | SessionStart hook for prox-L1 card handoff; interim fleet. | r:PH-OPERATOR | interacts with S7/SU hooks later |
| routing:OS-200 | INDEPENDENT | - | Pushes the Postgres+pgbackrest image to Harbor for the keeper deploy, a P1 deliverable (server:RT-P1-PG-DEPLOY). | r:PH-OPERATOR | close candidate: its target server:RT-P1-PG-DEPLOY is done (L1 review) |
| routing:OS-201 | INDEPENDENT | - | operator or server ops step, unrelated to the rethink design | r:PH-OPERATOR | - |
| routing:OS-202 | INDEPENDENT | - | operator or server ops step, unrelated to the rethink design | r:PH-OPERATOR | - |
| routing:OS-203 | INDEPENDENT | - | operator or server ops step, unrelated to the rethink design | r:PH-OPERATOR | - |
| routing:OS-205 | INDEPENDENT | - | operator or server ops step, unrelated to the rethink design | r:PH-OPERATOR | - |
| routing:OS-206 | INDEPENDENT | - | operator or server ops step, unrelated to the rethink design | r:PH-OPERATOR | - |
| routing:OS-207 | INDEPENDENT | - | operator or server ops step, unrelated to the rethink design | r:PH-OPERATOR | - |
| routing:OS-208 | REPARENT | routing:RT-P1 | Mints Temporal TLS certs; server:RT-P1-TEMPORAL-CHECK waits on it (D-1513), P1 critical path. | r:PH-OPERATOR | add as dep of server:RT-P1-TEMPORAL-CHECK |
| routing:OS-31 | INDEPENDENT | - | operator or server ops step, unrelated to the rethink design | r:PH-OPERATOR | - |
| routing:OS-33 | INDEPENDENT | - | Deferred push credential, needed only if the opencode fallback container is used. | r:PH-OPERATOR | cancel candidate if T4-SWITCH stays cancelled; operator confirm |
| routing:OS-41B | INDEPENDENT | - | operator or server ops step, unrelated to the rethink design | r:PH-OPERATOR | - |
| routing:OS-51 | INDEPENDENT | - | operator or server ops step, unrelated to the rethink design | r:PH-OPERATOR | - |
| routing:OS-52 | INDEPENDENT | - | operator or server ops step, unrelated to the rethink design | r:PH-OPERATOR | - |
| routing:OS-53 | INDEPENDENT | - | operator or server ops step, unrelated to the rethink design | r:PH-OPERATOR | - |
| routing:OS-54 | INDEPENDENT | - | operator or server ops step, unrelated to the rethink design | r:PH-OPERATOR | - |
| routing:OS-55 | INDEPENDENT | - | operator or server ops step, unrelated to the rethink design | r:PH-OPERATOR | - |
| routing:OS-61 | INDEPENDENT | - | operator or server ops step, unrelated to the rethink design | r:PH-OPERATOR | - |
| routing:OS-62 | INDEPENDENT | - | Optional free-provider signups; fits D7 free tiers. | r:PH-OPERATOR | - |
| routing:OS-63 | INDEPENDENT | - | operator or server ops step, unrelated to the rethink design | r:PH-OPERATOR | - |
| routing:OS-64 | INDEPENDENT | - | operator or server ops step, unrelated to the rethink design | r:PH-OPERATOR, server:OMNIROUTE-AUTHELIA-API | - |
| routing:OS-65 | INDEPENDENT | - | operator or server ops step, unrelated to the rethink design | r:PH-OPERATOR | - |
| routing:OS-68 | INDEPENDENT | - | operator or server ops step, unrelated to the rethink design | r:PH-OPERATOR | - |
| routing:OS-70B | INDEPENDENT | - | operator or server ops step, unrelated to the rethink design | r:PH-AUDIT-1002 | - |
| routing:OS-71 | INDEPENDENT | - | operator or server ops step, unrelated to the rethink design | r:PH-OPERATOR | - |
| routing:OS-73 | INDEPENDENT | - | operator or server ops step, unrelated to the rethink design | r:PH-OPERATOR | - |
| routing:OS-82 | INDEPENDENT | - | Provider debt-safety steps; supports D7 spend rules. | r:PH-OPERATOR | - |
| routing:OS-91 | INDEPENDENT | - | Antigravity permission cleanup; agy now uses Antigravity quota (design D25, s5). | r:PH-OPERATOR | unsure: restoring Antigravity settings may break RT-P2-AGY; check first |
| routing:OS-96 | INDEPENDENT | - | operator or server ops step, unrelated to the rethink design | r:PH-OPERATOR | - |
| routing:OS-99 | INDEPENDENT | - | operator or server ops step, unrelated to the rethink design | r:PH-OPERATOR | - |
| routing:OW-CLAIMS-VERIFY | INDEPENDENT | - | Claim-by-claim verification of pasted RC/Agent Teams text; design W1-W3/E1 already verified much of it. | r:PH-OPENWORK-1013 | unsure: possible duplicate of W1-W7/E1; dep OW-COMMS-MEASURE |
| routing:OW-COMMS-MEASURE | INDEPENDENT | - | Measures cross-device agent comms; may feed the planner-wake decision in RT-P3-PHONE (design s6). | r:OW-CLAIMS-VERIFY, r:PG-IMPROVE-PROPOSAL-1013, r:PH-OPENWORK-1013 | unsure: operator marked VERY IMPORTANT; W2 covers Claude side; could REPARENT routing:RT-P3 |
| routing:OW-OPENWORK-VS-OPENHANDS | CANCEL | design D16 / D17 / routing:RT-P5-OPENHANDS | Overview UI is the SvelteKit page (D16); OpenHands kept as harness with A/B in RT-P5-OPENHANDS (D17); Claude-limit fallback moot (D2). | r:PG-IMPROVE-PROPOSAL-1013, r:PH-OPENWORK-1013 | drop edges from PG-IMPROVE-PROPOSAL-1013 and PH-OPENWORK-1013 |
| routing:PG-IMPROVE-PROPOSAL-1013 | CANCEL | design D-1285 / routing:RT-P0-TRIAGE | Plan-graph improvement proposal for autoos/routing/opencode is superseded wholesale by the approved rethink design and this triage. | plangraph:PG-MERGE-ROUTING-PLAN, r:PH-OPENWORK-1013 | re-point plangraph:PG-MERGE-ROUTING-PLAN and PH-OPENWORK-1013 |
| routing:PH-AUDIT-1002 | INDEPENDENT | - | Audit gap gate with mixed open deps (ops, autoos, server). | r:GOAL | cancel only if all deps cancelled |
| routing:PH-EBN-1003 | INDEPENDENT | - | ebn diagnostics science gate; unrelated. | r:GOAL | - |
| routing:PH-FLEET-1003 | INDEPENDENT | - | Fleet-operations gate with many autoos deps; most decided in the autoos chunk. | r:GOAL | dep autoos:O2-FLEET-BUS-MCP named overlap (verdict in autoos chunk); cancel if all deps cancelled/covered |
| routing:PH-GEN-1003 | INDEPENDENT | - | gen-analysis product gate; unrelated. | r:GOAL | - |
| routing:PH-OPENWORK-1013 | INDEPENDENT | - | D-1277 gate; two deps cancelled here, others open. | r:PH-OPPLAN-1012 | remaining deps PG-MERGE-ROUTING-PLAN, OW-COMMS-MEASURE, OW-CLAIMS-VERIFY; cancel if all go |
| routing:PH-OPERATOR | INDEPENDENT | - | Container gate of all operator questions and steps. | r:GOAL | - |
| routing:PH-OPPLAN-1012 | INDEPENDENT | - | Operator plan gate; three deps cancelled here, others open. | r:GOAL | S19 drops edge to autoos:AO-CTX-RECYCLE-ALLHOSTS; drop edges to COORD-DIET-PILOT, DECISION-MODELS-RESEARCH, SKILLS-HOME-PROPOSAL; cancel if all go |
| routing:PH-RETHINK | INDEPENDENT | - | The rethink gate itself; keep. | r:SETUP-IMPROVEMENT-PLAN | reaches GOAL only via SETUP-IMPROVEMENT-PLAN; SU-DOCKER depends on it |
| routing:PH-SERVER-1003 | INDEPENDENT | - | Server ops gate; unrelated. | r:GOAL | - |
| routing:Q-CH-FIX-LIVE | INDEPENDENT | - | operator or server ops step, unrelated to the rethink design | r:PH-OPERATOR | - |
| routing:Q-CH-UP77-PR | INDEPENDENT | - | operator or server ops step, unrelated to the rethink design | r:PH-OPERATOR | - |
| routing:Q-FRITZ-IPV6 | INDEPENDENT | - | operator or server ops step, unrelated to the rethink design | r:PH-OPERATOR | - |
| routing:Q-OMNIGRAPH-HOP-TLS | INDEPENDENT | - | operator or server ops step, unrelated to the rethink design | r:PH-OPERATOR | - |
| routing:Q-OMNIGRAPH-WS | INDEPENDENT | - | operator or server ops step, unrelated to the rethink design | r:PH-OPERATOR | - |
| routing:Q-RELAUNCH-WORKST | INDEPENDENT | - | operator or server ops step, unrelated to the rethink design | r:PH-OPERATOR | - |
| routing:Q-SPLIT-DNS | INDEPENDENT | - | operator or server ops step, unrelated to the rethink design | r:PH-OPERATOR | - |
| routing:Q-VERTEX-QUOTA | INDEPENDENT | - | operator or server ops step, unrelated to the rethink design | r:PH-OPERATOR | - |
| routing:Q-WSCLONE-REPOINT | INDEPENDENT | - | operator or server ops step, unrelated to the rethink design | r:PH-OPERATOR | - |
| routing:REFERRAL-DOCS | INDEPENDENT | - | Docs chore for a referral link; unrelated. | r:PH-AUDIT-1002 | - |
| routing:ROUTE-400LOC | INDEPENDENT | - | Cross-project coding-principles rule; valid as is (Rust crate also <= 400 lines). | r:SETUP-IMPROVEMENT-PLAN | - |
| routing:ROUTE-CHANGELOG-DOCS | INDEPENDENT | - | Cross-project changelog/docs rule; valid as is. | r:SETUP-IMPROVEMENT-PLAN | - |
| routing:ROUTE-CLEF-SHADOW | CANCEL | design D9 / D18 / routing:RT-P5-JEV | Shadow pre-processor targets past L0 router messages; persistent L0 is removed (D9) and shadow decision models are RT-P5-JEV (D18). | r:SETUP-IMPROVEMENT-PLAN | dependent SETUP-IMPROVEMENT-PLAN |
| routing:ROUTE-DEPTH-RESEARCH | CANCEL | design s1/s13, research R1-R5, W1-W7, D9 | Orchestration depth and stacked lenses already researched and decided: state+code, no persistent layers. | r:ROUTE-SKILLS, r:SETUP-IMPROVEMENT-PLAN | dependents ROUTE-SKILLS, SETUP-IMPROVEMENT-PLAN |
| routing:ROUTE-JEV-TRIAL | CANCEL | design D18 / routing:RT-P5-JEV | Decision-model layer and jev trial are replaced by rules deciding with free Jev in shadow, evaluated in RT-P5-JEV. | r:SETUP-IMPROVEMENT-PLAN | named overlap; dep ROUTE-COMBO-PROPOSAL; dependent SETUP-IMPROVEMENT-PLAN |
| routing:ROUTE-L2-LAYER | CANCEL | design D9 / s10 deletions | Enforcing L1->L2->L3 layers is superseded: no persistent L0/L1/L2 sessions; opencode orchestrator lanes deleted. | r:SETUP-IMPROVEMENT-PLAN | dependent SETUP-IMPROVEMENT-PLAN; deps AO-L2-* decided in autoos chunk |
| routing:ROUTE-PG-PRODTEST | CANCEL | design s9 / routing:RT-P0-BASELINE / RT-P2-M1 | Prodtest measures live L1s with task_key; replaced by baseline metrics job and M1 measurement on the new pipeline. | r:ROUTE-PG-SSOT | live dependent ROUTE-PG-SSOT: drop edge and reword its done |
| routing:ROUTE-PG-SSOT | INDEPENDENT | - | Plan graph as single source stays valid; keeper is the SSOT in the design. | r:SETUP-IMPROVEMENT-PLAN | named overlap: dep autoos:AO-PG-MCP-WIRE cancelled, re-point to plangraph:SU-MCP (orch-setup s9); drop PRODTEST dep |
| routing:ROUTE-SKILLS | INDEPENDENT | - | Skills import/research profile; skills now move to plangraph (D13, RT-P4-SKILLS-REWRITE). | r:SETUP-IMPROVEMENT-PLAN | unsure: 'import to AutoOS' contradicts D13; dep ROUTE-DEPTH-RESEARCH cancelled; consider fold into RT-P4-SKILLS-REWRITE |
| routing:ROUTE-WRITER-OPS | INDEPENDENT | - | Server ops writers first-pass quality; relates to D8 checks but tied to autoos writer guards. | r:SETUP-IMPROVEMENT-PLAN | unsure: depends on AO-WRITER-GUARDS/AO-LEDGER-* verdicts in autoos chunk |
| routing:RT-P0 | INDEPENDENT | - | RT gate itself; keep. | r:PH-RETHINK, r:RT-P1 | - |
| routing:RT-P0-BASELINE | INDEPENDENT | - | RT item itself; keep. | plangraph:RT-P1-GOVERNOR, r:RT-P0 | - |
| routing:RT-P0-FREEZE | INDEPENDENT | - | RT item itself; keep. | r:RT-P0 | - |
| routing:RT-P0-MDSYNC | INDEPENDENT | - | RT item itself; keep. | r:RT-P0 | - |
| routing:RT-P0-TRIAGE | INDEPENDENT | - | This triage item; in progress. | r:RT-P0 | - |
| routing:RT-P1 | INDEPENDENT | - | RT gate itself; keep. | plangraph:RT-P2-AGY, plangraph:RT-P2-DECISIONS, plangraph:RT-P2-QODER, r:PH-RETHINK, r:RT-P2 | OS-196/200/208 REPARENT here (via their server/plangraph targets) |
| routing:RT-P2 | INDEPENDENT | - | RT gate itself; keep. | plangraph:RT-P3-SVELTE, plangraph:RT-P4-LEDGER-CUTOVER, plangraph:RT-P4-ROUTER-CODE, plangraph:RT-P4-SPAWNER, r:PH-RETHINK, r:RT-P3, r:RT-P4, server:RT-P4-INFRA-MCP | - |
| routing:RT-P2-M1 | INDEPENDENT | - | RT item itself; keep. | r:RT-P2 | - |
| routing:RT-P3 | INDEPENDENT | - | RT gate itself; keep. | plangraph:RT-P5-JEV, plangraph:RT-P5-OPENHANDS, plangraph:RT-P5-OR-MCP, plangraph:RT-P5-PLUGINS, plangraph:RT-P5-UNATTENDED-PLAN, r:PH-RETHINK, r:RT-P5, r:RT-P5-PROJECT2 | UI-FLEET-TAB and Q-API-PUSH chain join it (orch-setup) |
| routing:RT-P4 | INDEPENDENT | - | RT gate itself; keep. | plangraph:RT-P5-JEV, plangraph:RT-P5-OPENHANDS, plangraph:RT-P5-OR-MCP, plangraph:RT-P5-PLUGINS, plangraph:RT-P5-UNATTENDED-PLAN, r:PH-RETHINK, r:RT-P5, r:RT-P5-PROJECT2 | - |
| routing:RT-P4-ARCHIVE | INDEPENDENT | - | RT operator task itself; keep. | r:RT-P4 | - |
| routing:RT-P5 | INDEPENDENT | - | RT gate itself; keep. | plangraph:RT-P6-RUST-KEEPER, r:PH-RETHINK, r:RT-P6, server:RT-P6-MOVE | - |
| routing:RT-P5-PROJECT2 | INDEPENDENT | - | RT item itself; keep. | r:RT-P5 | - |
| routing:RT-P6 | INDEPENDENT | - | RT gate itself; keep. | r:PH-RETHINK | - |
| routing:SETUP-IMPROVEMENT-PLAN | INDEPENDENT | - | Container lane that already depends on PH-RETHINK; keeps GOAL linked to the rethink. | r:GOAL | after cancels, remaining deps are autoos AO-* and ROUTE-400LOC/CHANGELOG/PG-SSOT; do not cancel while GOAL depends on it |
| routing:SKILLS-HOME-PROPOSAL | CANCEL | design D13 / s10 / routing:RT-P4-SKILLS-REWRITE | Skills home is decided: plangraph/skills; unattended-orchestration rewritten in RT-P4-SKILLS-REWRITE, spawner moves in RT-P4-SPAWNER. | r:PH-OPPLAN-1012 | drop edge from PH-OPPLAN-1012 |
| routing:T4-CERT | CANCEL | design s11 RT-P1-E2E / routing:RT-P2-M1 | 12 h certification of router+L1+L3 inside opencode is superseded: no persistent router/L1; E2E and M1 targets replace it. | autoos:T5-CERT, r:GATE-P3, r:T4-SWITCH | live dependents autoos:T5-CERT, GATE-P3, T4-SWITCH: drop edges |
| routing:T4-SWITCH | CANCEL | design D2 / s4.5 / s9 governor | Claude-limit takeover by an opencode fleet is moot: Claude is planner+judge only; usage_limit re-route/timers and the governor handle limits. | r:GATE-P3 | live dependent GATE-P3: drop edge |
| routing:T7-GRAPH-R | INDEPENDENT | - | Routing repo D-ids into omnigraph; omnigraph stays the Knowledge tab, but D-rows move to the keeper table. | r:GATE-P3 | unsure: sync source (md ledgers) archived at RT-P4-LEDGER-CUTOVER; cancel candidate; dependent GATE-P3 |
| routing:TEST-HARNESS-PROPOSAL | INDEPENDENT | - | Test harness proposal for MCP, skills, opencode; replay/contract tests are still wanted (s12). | r:PH-OPPLAN-1012 | proposal only; may feed RT-P4-SKILLS-REWRITE evals |
