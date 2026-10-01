# Plan status — 2026-10-01 ~14:45Z (router-verified where marked ✓; others = orchestrator reports)

## What is done today (since the plan v3 re-plan at 12:30Z)
| Item | State | Evidence |
|---|---|---|
| Router back on Claude, stale 09-28 sessions held + stopped, new L1-mains (Sonnet high) | done ✓ | D-180, D-186; sessions 8cd0f9b4, cdd9f6da |
| Ground truth audit (5 investigations) + plan v3 + briefs | done ✓ | research/2026-10-01-ground-truth.md, docs/plan/ |
| opencode container recreated with init, pids 32768, mem 8g | done ✓ | docker inspect Init=true (D-194) |
| OS-35 key alias → gateway spawns work | done ✓ | count 1 |
| Workstation: 7 redundant branches deleted (bundle backup), clones on origin/main | done ✓ | D-183, workst reports |
| Tailnet edge (OPNsense tailscale0, rules, Unbound, Caddy LAN trust) + split DNS | done ✓ | D-192, workst: healthz 200 over tailnet |
| Secrets off coding.vm (sparse-checkout), comicvine imported; SOPS decrypt path proven | done (prox) | Server ec1b625, 0167ab4; Semaphore task 127181 |
| Research: Open WebUI/SearXNG, dockhand, SearXNG-MCP, OmniRoute caching | done ✓ (13 + 4 URLs fetched) | D-196, D-198, D-199 |

## Lanes in flight
| Lane | State | Blocker / next |
|---|---|---|
| T0-PAID(-2), T0-FREEZE, T0-BUDGET(-2), T1-CREDIT-FIX, KEYNAME | writers done (KEYNAME finishing), reviews running | merge as one train behind T3-CIGREEN |
| T3-CIGREEN | running | ~40 more deepseek-pinned tests after the paid flip |
| T1-CLEAN (private tier: vertex + ovh + local GPU) | queued, priority after the train | blocks every site-data lane (server T6-*) |
| T1-WEBTOOLS + T2-RESEARCHCARD | designed (SearXNG + MCP) | waits for the SearXNG deploy (T6-OWUI) |
| T2-XMSG + T2-XSECRET + T7-CONSOLE-MSG | new (D-203), priority after T1-CLEAN | fleetd/bus spec exists (gap 2) |
| T5-GROK lane 1 (hardening) | running (workstation, local repo) | then provider + Funnel; trial end date from the operator |
| T5-GRAPH (workstation → central graph) | ready | operator runs one line in workst-L1's session (credential, D-201) |
| T6-GRAPHMOVE (omnigraph → cloud.vm) | queued | T1-CLEAN; baseline taken (research/2026-10-01-omnigraph-baseline-autoos.md) |
| T6-IPV6 (V1) | approved, Fritzbox user ready | prox applies |
| T6-SECRETS (SOPS + age + rotation) | migrating (playbooks next) | rotations in per-application batches with operator approval (D-203) |
| T6-OWUI, T6-UPDATES, T6-PILOT, T6-DETECT | research done; implementation queued | T1-CLEAN |
| T4-CERT / T4-SWITCH (opencode 4-level certification + automatic fallback) | not started | after T4-HARDEN/HOST/FENCE/MSG |

## Orchestrator status (reported 14:00–14:05Z; nothing merged to main yet — main CI red, freeze holds)
| Track | State | Est. left (orchestrator, inferred from measured run times) |
|---|---|---|
| T0 | SYNC done; PAID v1 verified → split into 2a (enforcement, running) / 2b (last-resort tier + deepseek 3-test gate) / 2c (meta review-only); FREEZE needs a fix round; BUDGET-2 running | ~8 lane-steps · ~5 h · ~0.6 M |
| T3 | CIGREEN queued behind PAID-2 (~40 deepseek-pinned + ~25 other reds); SCRUB already on main (1738258f) | ~10 · ~8 h · ~0.9 M |
| T1 | CREDIT-FIX verified (reviews pending); KEYNAME finishing; CLEAN, COMBO3 (+C10/C10a/C11), CTX, UPGRADE, PROV, WEBTOOLS, MM, PEER, LITELLM queued | ~18 · ~20 h · ~2 M |
| T2 | all queued (ONEPATH, RECORD, TASKSESSION, XMSG, XSECRET, BUDGET, CUTRUN, WATCH, RULES, SKILL, COMPACT, CANARY, LEAN, RESEARCHCARD, MCPLIFE, ISOLATE) | ~16 · ~24 h · ~2 M |
| T4 | HARDEN queued (container already recreated by the router); HOST/FENCE/MSG/ZEN/CERT/SWITCH queued; CERT+SWITCH = router drills | ~8 · ~10 h · ~0.8 M |
| T6 (server-L1-main) | OWUI + UPDATES + SearXNG research done; PILOT/DETECT ready; all site-data lanes wait for T1-CLEAN; INFRA blocked on T-040; PHONE needs an edge OK | ~1.1–1.5 M · 5–8 working days (+3–5 for HEAL/FIX/LEDGER) |
| T6-SECRETS / T6-IPV6 (prox) | SOPS decrypt proven; playbook migration next; IPv6 V1 applying | — |
| T7 | GRAPH-A/USE/CONSOLE(+MSG) queued; BOARD waits for the operator's choice | ~6 · ~8 h · ~0.6 M |
Bottlenecks: free-leg writer throughput, reviews serialised until reviewer pools have ≥ 3 families (FAMILYFENCE), T1-CLEAN gating all server work.

## Remaining effort estimate (router, inferred; refined from orchestrator reports)
| Track | Lanes left | Claude tokens (Sonnet, orchestration + finals) | Wall time | Paid cost |
|---|---|---|---|---|
| T0 + T3 (stabilize, green main, CI policy, cleanup) | ~10 | 3–5 M | 1–2 days | $0 |
| T1 (combos v3, context, clean tier, providers, credits, Zen, LiteLLM, peer, web tools, multimodal) | ~14 | 6–9 M | 4–6 days | trial credits (vertex ≤ $250, ovh ≤ $200) + credits (~$25) |
| T2 (one path, records, budgets, cut-run, watch, rules, skill v4, compaction, XMSG, XSECRET, research card, MCP life, isolate) | ~16 | 8–12 M | 6–8 days | $0 |
| T4 (harden, host heartbeat, fences, messaging, Zen, certification ×2, fallback switch) | 7 | 3–5 M | 4–5 days | $0 |
| T5 (workstation) | ~8 | 2–3 M | 2–3 days | Grok trial only |
| T6 (server ops: gateway, self-healing, Open WebUI, voice, updates, secrets, IPv6, graph move) | ~15 | 6–9 M | 2–3 weeks | $0 (trial/free models) |
| T7 (graphs, board choice, console P2–P4 + messaging pages) | ~6 | 6–9 M | 2–3 weeks | $0 |
| **Total** | **~76** | **≈ 8–10 M (orchestrator estimates) … ≈ 35 M (09-28 measured baseline of 1.4–4 M per merge)** | **≈ 2–3 weeks** with 3 L1-mains in parallel (T0–T4 ≈ 1 week, T6/T7 run alongside) | **≤ $25/month paid + trial credits; Claude = Max subscription (flat)** |

Paid last resort (D-195): deepseek-v4.1-flash only when all free/trial legs fail; expected ≈ $0–5/month.
Claude: weekly subscription limit is the real budget — ~35–55 M weighted tokens over ~4 weeks ≈ 9–14 M/week across
router + 3 L1-mains (Sonnet), within a Max 20x week if workers stay on free/trial legs.
