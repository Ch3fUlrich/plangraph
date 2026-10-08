# Build plan v3 — 2026-10-01 (supersedes the build order in `docs/PLAN.md`; its architecture sections stay valid)

Owner: routing-00 (L0). Evidence base: [`../../research/2026-10-01-ground-truth.md`](../../research/2026-10-01-ground-truth.md).
Every lane below is written so an agent can execute it without asking: inputs, steps, DONE test, reviews, model tier.
Only items marked **OPERATOR** need a human (credentials, money beyond credits, taste/architecture gates).

## Files
| File | Track |
|---|---|
| [`T0-stabilize.md`](T0-stabilize.md) | stop the bleeding: costs, container, CI freeze, secrets, repo sync, stale sessions |
| [`T1-gateway-models.md`](T1-gateway-models.md) | OmniRoute combos v3, providers, credits, Zen, LiteLLM standby, peer chaining, multimodal |
| [`T2-orchestration-core.md`](T2-orchestration-core.md) | spawner MCP as the single decision path, mechanical rules into code, skill restructure, compaction |
| [`T3-ci-quality.md`](T3-ci-quality.md) | CI only on main, green main, local-green, clutter removal, DRY, 400-line rule |
| [`T4-opencode-fleet.md`](T4-opencode-fleet.md) | container hardening, host heartbeat, fences, messaging, certification, Claude→opencode fallback |
| [`T5-workstation.md`](T5-workstation.md) | workst-L0/L1, fleet clones, server-as-source + local fallback, branch merges, Grok Bot |
| [`T6-server-ops.md`](T6-server-ops.md) | homelab gateway (prox), self-healing, Open WebUI + voice, Home Assistant, phone |
| [`T7-memory-console.md`](T7-memory-console.md) | omnigraph memory for AutoOS + routing, fleet console P2–P4, board choice |
| [`TESTING.md`](TESTING.md) | the test ladder every lane uses, production tests, certification scenarios |

## Principles (binding for every lane)
1. **Mechanics in code, judgement in agents.** Spawn decisions, tier/model choice, review counts, budgets, heartbeats and
   push/merge gates are enforced by `autoos-agent` (MCP + CLI, one code path). Agents never pick models by hand.
2. **Evidence over claims.** A DONE line names: the commit sha, the test command + pass counts, the review records
   (reviewer model + family + run id), and the **model that did the work** (provider/model/family). No evidence = not done.
3. **Two reviewers from two other model families + a Sonnet final** for every implementation, plan and spec (D-115,
   D-167). Reviews run on free/trial legs first.
4. **Local-green first.** Lanes run affected tests while working and the full suite once right before merge; GitHub CI
   runs on `main` only. No merge while main is red, except the lane that makes main green.
5. **Constraints live in files, not in conversation.** Every session re-reads its card/brief (goal, DONE, deny-lists) at
   start and after every compaction; checkpoint to the card at ~65 % context fill.
6. **Single source of truth, DRY, ≤ 400 lines per file** (configs split into folders when larger). Generated files carry
   their generator; prose never copies tables that code can render.
7. **Fan out, don't stall:** orchestrators send cheap L3 researchers/readers (free legs, rotating providers) to search
   files and the web, and keep their own turns for judgement; a rate-limited big agent never does lookups itself.
8. **Public repo hygiene:** no hostnames, usernames, keys or site data in AutoOS; site specifics live in `routing` or
   local config. The scrub gate in CI stays.

## Model and level policy (operator 2026-10-01)
| Level | Runs as | Model |
|---|---|---|
| L0 router | Claude Code `routing` (coding.vm); backup `workst-L0-autoos` (workstation) | Sonnet, low/medium effort |
| L1 main orchestrators | Claude Code: `autoos-L1-main` (coding.vm), `server-L1-main` (Server repo), `workst-L1-autoos` (workstation) | Sonnet, high effort |
| L2 track orchestrators | spawned via `autoos-agent` MCP (opencode client) | tier order below |
| L3 workers / reviewers / researchers | spawned via `autoos-agent` MCP (opencode, qoder, agy …) | tier order below |

**L2/L3 tier order: trial → free → credits → paid.**
- Trial: vertex_ai ($250, 90 days) and ovhcloud ($200, ~1 month). Use them first and efficiently before they expire.
- Free: opencode Zen with our own key, inside opencode only; OpenRouter `:free`; Groq; qoder Qwen3.8-Flash; Mistral
  Codestral; LLM7; Z.AI GLM-Flash; Cloudflare; Scaleway and the other free legs.
- Credits: morph $10, deepinfra $5, together $5 (key renewed), nscale $5.
- Paid: Sonnet only, as the last fallback (Claude subscription for finals).
- **Paid otherwise stopped:** DeepSeek and Meta Muse Spark contributor are never chosen while a free/trial leg is healthy (D-195).

**Opus:** on demand only, for hard design or security reviews.

**Last resort (D-195):** when every free and trial leg of a class is rate-limited or down, `deepseek-v4.1-flash`
implements (resolver-checked and logged), reviewed by `muse-spark-1.3-contributor` + a Sonnet final. Unavailable
free models never block work.

**Operator commands (D-194):** the plan never relies on the operator running commands; agents execute what their
permissions allow, and the operator only approves critical items (yes/no) or acts where only their accounts can.

## Phases and gates
| Phase | Lanes | Gate to the next phase |
|---|---|---|
| 0 now | T0-* | paid legs off; container on init; main CI fix lane started; stale sessions held; keys rotation scheduled |
| 1 | T3-CI*, T1-COMBO3, T1-CTX, T4-HOST, T4-FENCE, T2-ONEPATH, T2-RECORD | main green 3 pushes in a row; combos v3 live with drift 0 + smoke; no forced compaction in 24 h |
| 2 | T1-PROV*, T1-LITELLM, T1-PEER, T2-RULES, T2-SKILL, T5-*, T6-PILOT, T6-VOICE | every provider row probed; skill v4 merged; workstation on server gateway |
| 3 | T4-CERT, T4-SWITCH, T6-HEAL, T7-* | opencode fleet passes certification twice; fallback switch tested by a forced Claude-limit drill |

Parallelism: at most one control-plane lane (spawner/MCP/registry/gateway apply) at a time; docs, tests, research and
workstation lanes run in parallel within the memory/process budget (`T4`).

## Ownership and channel
- routing-00 talks only to the three L1-mains (AutoOS, server, workstation) and to `workst-L0-autoos` for failover; each
  L1-main runs its track lanes through spawned L2/L3 agents and never implements itself (R-coord-14).
- Orders: SendMessage (Claude) or inbox line + `bin/fleet-wake` (opencode). Replies: inbox/L0.md or SendMessage.
- Questions to the operator only for taste/architecture/credentials/spend; the router batches them interactively.

## Open operator gates (taste / architecture / credentials)
- OPERATOR OS-31 rotate every key in `configuration/api-keys.yml` (leaked into model transcripts 09-29).
- OPERATOR recreate the opencode container (command in T0-CONTAINER) — approved "now".
- OPERATOR board choice after T7-BOARD evaluation; HolmesGPT adoption after T6-HEAL pilot result.
- OPERATOR T-040/MT-233 Semaphore admin token (unblocks homelab gateway phase 3).
