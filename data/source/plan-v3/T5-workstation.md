# T5 — Workstation (Windows + WSL2): backup router, clean merges, server as the source

Sessions: `workst-L0-autoos` = backup L0 router (relays only while routing-00 is alive; takes over when coding.vm or
routing-00 is down for > 15 min, checked by its own heartbeat against routing-00's last beat); `workst-L1-autoos` =
workstation L1-main (Sonnet high), spawns L2/L3 via the local `autoos-agent` MCP in WSL fleet clones.
Server (coding.vm) is the main source of routing (combos, registry, plans); the workstation follows `origin/main`.

| Lane | What | DONE (evidence) |
|---|---|---|
| T5-FINISH | Let the 3 running opencode sessions finish their in-flight items (OVH registry key rename, RENDERFIX attempt 2) on a FREE/trial model (switch off paid deepseek now); verify each result (tests, local-green, pushed?) | each item: sha + tests + review records, or abandoned with reason |
| T5-CLONE | All agent work in `~/fleet/AutoOS` (WSL, primary) or `C:\fleet\AutoOS` (native pwsh tests only); both fast-forward to origin/main (the Windows clone was 347 behind) | `git status` of both = origin/main; no agent cwd in the operator checkout |
| T5-BRANCHES | Merge candidates: ws-merge-map (take 65c2d0af, not 9e9c39b7), ws-records, ws-mergecheck, ws-scrubgap (after local CI); hold ws-remote/WS-OMNIREMOTE until its fleet-clone fixes (891d4e6 + 4 commits) pass r4e + local-green + reviews; drop ws-mainleak; keep ws-nebius-combos unmerged (username) until an operator decision is not needed: summarise its unique doc into main and delete | each branch: merged sha or deleted with bundle backup; table in the L0 inbox |
| T5-KEYS | Workstation key file: `omniroute_server` (server-issued) + `omniroute_workstation` (local) in the WSL fleet clone (done by operator) and the native clone; reader code uses the gateway-specific name (D-147/D-148) | WS-OMNIREMOTE smoke: one call through the server gateway from Windows and from WSL |
| T5-SYNC | Local OmniRoute config rendered from the same registry/combos as the server (T1-COMBO3) + peer leg to the server (T1-PEER); drift check runs on the workstation daily | drift 0 against the repo; server-down drill served locally |
| T5-PROVIDERS | Verify vertex_ai and ovhcloud really consume credit (spend visible in GCP billing/OVH console or gateway cost ledger), from the workstation and from the server | spend delta > 0 after a test batch; documented in T1-CREDIT report |
| T5-SKILLS | Review the new skills (`combo-create`, updated `unattended-orchestration`): no hardcoded live data, no site data, ≤ 400 lines, rules have tests | review records from 2 families; fixes merged |
| T5-HANDOFF | One current `docs/handoff.md` for the workstation (successor brief: start / what is true / work in order / not yours / done means), older handoff files archived (T3-CLEAN) | file ≤ 400 lines; points to plan v3 lanes |
| T5-CERT | Run the opencode certification (T4-CERT) on the workstation's opencode too (native Windows + WSL) | scorecard |
| T5-GRAPH | Workstation uses the CENTRAL omnigraph (not the stale local docker on :8080): find the reachable URL over the tailnet; operator sets the Windows User env OMNIGRAPH_BASE_URL (+ token); local graph becomes a read-only cache or stops | whoami check (AutoOS CLAUDE.md) returns 1 row on the workstation |
| T5-GROK | Grok Bot (operator: Tailscale Funnel or not public; async only): retrieve the FastAPI wrapper + README from the Grok conversation (operator pastes it into the repo or a gist — the code is not on any host), run it on the workstation behind a Tailscale Funnel path with a sender secret, register as OmniRoute custom provider `grokbot` with its own combo (C9: no fallback, timeouts ≥ 700 s, retries 0, Protect off), async mode only | OPERATOR provides the wrapper code; smoke: async job id → result callback → job complete; grokbot never in another combo |

Failover rule (workst-L0): when routing-00 is unreachable for > 15 min, workst-L0 sends ONE message to the operator,
takes the router role from `routing/status/routing-00.card.md` (pulled from origin) and keeps lanes moving on the
workstation; it hands back when routing-00 answers. Two routers never give orders at the same time.
