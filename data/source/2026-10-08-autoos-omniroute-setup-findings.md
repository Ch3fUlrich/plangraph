# omniroute_setup session: findings report

Source: `omniroute_setup_digest.md` (3419 lines, all read). The session ran from about 2026-09-30 to 2026-10-07 and is cited below by digest line number (Lnnn). No secrets appear in the digest. Key values were never printed in the session, and the digest already shows any that existed as `<r>`.

## 1. Goal of the session

The operator opened the session by asking the agent to act as "workstation-L1-main" under the `unattended-orchestration` skill (L3). That meant spawning interactive L1 sessions, which in turn spawn L2/L3 lanes on **free models from many providers**, not one OmniRoute combo, and improving those combos. The opening request also listed these jobs:
- Clean up stale worktrees.
- Use Serena and Graphify.
- Use vertex_ai again, since the operator had fixed it.
- Research a fallback to the remote gateway `https://omniroute.ohje.ooguy.com`.
- Fix the deepseek-flash error "`reasoning_text` … must be passed back" (HTTP 400).
- Make sure about 47 listed providers are present with their models (L66-119).

Later directives widened the scope:
- Combo contexts should show the real maximum capacity, and `chat_admission_busy` needed a fix (L150-151).
- Every solution must be reviewed by 2-3 model families (L507, L1033).
- Add a researcher tier, but only if it makes sense.
- Answer a 10-item design memo (L155-177).
- L0/L1 must be interactive top-level sessions, and L1/L2 delegate instead of fixing (L208-212, L299).
- Use the OVH $200 trial in the combos (L542). Remove Nebius after a $350 debt (L658).
- Use the order **trial → free → credits → paid, deepseek last** (L1125).
- Agents should handle merges and pushes (L1683).
- A layer redesign: rename t1/t2/t3 to L1/L2/L3, use OpenCode instead of Claude for orchestration, base the L1/L2/L3 combos on the Artificial Analysis intelligence index and cost per task, integrate Clef, and do literature research on multi-level orchestration (L2319-2373).
- Final asks: an availability and credits database, a dated reason for every combo leg, a full provider×model×combo table, and a cleanup of `docs/models-proposed.md` (L3200-3407).

## 2. FINDINGS (facts established)

**Gateway behaviour and bugs**
- `chat_admission_busy` came from the admission gate shedding with `reason=queue_timeout activeHeavy=1 waiting=0`, i.e. the heavy-slot cap was too small. The fix raised the slots from 1 to 4 (later 8 at restart). Evidence: gateway log (L349), F2/L1-alpha result showing 4/4×200 and zero busy (L497, L680).
- The gateway crashed around 14:49 local on 2026-09-30, with no OOM line and nothing restarting it. Separately, `start-stack.ps1:111`, `apply.ps1:97` and `configuration/autostart/Start-AutoOSStack.ps1:47` call bare `Start-Process 'omniroute'`, which resolves to the `.ps1` shim and fails. The autostart path also never set the admission knob. Evidence: L344-349, L777, L893.
- **Storage-key incident.** `~/.omniroute/.env` was rewritten with a 3-character `STORAGE_ENCRYPTION_KEY`, most likely by a fix lane. After that, 524 decrypt failures ("Auth tag validation failed") hit all 29 providers and caused a mass of 401s. The original key was never found. Recovery used a new key plus `providers rotate --from-env` for 28 connections. Evidence: L391-399, L437-441.
- `apply.ps1` skips connections that are already registered (lines 337-339), so it cannot refresh credentials (L421).
- The vertex leg returned 400 "Requests ending with a model turn are not supported" on agentic, tool-carrying turns. Because the gemini AI Studio leg was in 429 cooldown, failover hit this leg and killed both L1s (L273, L288). Fixed by F1 in the `openai-to-gemini.ts` package patch (L708).
- Vertex needs **service-account JSON** credentials, not an API key. The credential file must stay untracked, and a `.gitignore` rule plus test were added (L372, L1337, L1376).
- A per-model `max_completion_tokens` clamp of 16384 for scaleway qwen3-235b never applied, because of a gating condition `supportsThinking !== true` in the request builder. It now uses a live-bundle patch (L631-633). Direct provider-leg calls still bypass the clamp (L637).
- The gateway has **no per-leg timeout knob**. The breaker is the only fast-skip, and `maxWaitMs` is queue wait. The 22.5 s crawl to deepseek came from retries on dead legs (L1378). After the tier reorder (TORDER), the t1 probe took 6.7 s with 1 fallback on vertex, versus 22.6 s with 3 fallbacks before (L1390).
- Repeated-429 policy added to the launchers: rotate after 3×429 in 120 s, cool the leg for 300 s, park a hard-down provider for 30 min. It only loads from process env at startup (L1072-1073).
- Plain combo names work against the gateway directly. The `omniroute/` prefix is valid only inside opencode (L540, L582).
- **Compaction root cause.** The registry route context for `gemini-3.8-flash` said 128k. That rendered into `ide-models.json` and `opencode.jsonc` as 131072, so opencode compacted at 128k while the combo served 1M. Every render reverted the manual fix (L1187-1199).
- Patched gateway bundles revert on an npm upgrade, and reapply scripts exist (L635). A pre-restart audit found that vertex patch backups existed for only 1 of 4 chunks; they were later rebuilt and 19/19 revert paths were certified (L723, L1059).

**Provider and model availability** (dates 2026-09-30 to 10-07)
- Usage on 2026-09-30 (L756-766):

  | Provider | Calls | Notes |
  |---|---|---|
  | Nebius | 3,250 | The $350 debt; failover legs `nebius/zai-org/GLM-5.2` and `GLM-5.3-Flash` (L671) |
  | DeepSeek | 1,046 | |
  | Scaleway | 481 | 6,706 "credits exhausted" rejects |
  | Vertex | 194 | |
  | OVH | 183 | |
  | Gemini AI Studio | 16 | 7,242 cooling events and 6,766×429 |

- Leg-health matrix (redone version): 36 probes gave 25×200-with-content, 1 null 200, 6×401, 2×429 and 2×502 (meta-api `upstream_empty_response`). That is 24 usable completions. muse-spark returns null content at `max_tokens:8` and needs at least 256 (L824, L998).
- Qwen: `free-ai/qwen7b` works, and it is tiny and prone to fabrication. `scw` qwen fails above 16384 output tokens; scaleway was later credit-dead and removed (L481-487, L802).
- `t2-worker` lanes degenerated repeatedly, with fabrications and no-ops. `t3-driver-clean` went 3/3, but it is a deepseek combo (L1005, L1049).
- OVH: all six chat models answer through the `ovh/` and `ovhcloud/` prefixes. `Qwen3.8-27B` is 128k, not 262k (L570, L1379). Later, `gpt-oss-120b`, `Qwen3.5-397B` ($0.71/$4.25), `Meta-Llama-3_3-70B` (tool calls proven) and `Qwen2.5-VL-72B` (no registry row) were confirmed live (L3105).
- OVH and Vertex went unused because:
  - the lane pins sent all writers and readers to deepseek;
  - credit legs sat at positions 13-15 of 18;
  - `free-ai` came after deepseek;
  - vertex was missing from t2/t3;
  - the live store lagged the repo (L1134-1137).
- `openrouter/qwen/qwen3.8-27b:free` was withdrawn upstream (404) and removed (L2314, L2595). OpenRouter has no credits, so only `:free` legs are allowed, enforced by the gate (L1301-1311).
- HuggingFace is unusable and was removed. Antigravity is rate-limited and parked. navyai, bluesminds and arcee have no free models. Gemini AI Studio was kept, governed by the backoff policy (L1336, L1065).
- `glm-5.3-flash` via the opencode connection returns 402 "requires an opencode API key", so it is gated (L2390, L3068).
- `ainative/llama-4-maverick` ($9.79 trial) is available but LiteLLM can't serve it (L2561).
- The ZCode app (`ZCode.exe`, Electron) has no spawnable CLI, so the provider stays `available:false` (L3345).
- Devin:
  - `dva/*` returns 500 because `DEVIN_AGENTIC_HOME` must point inside the bridge sandbox, and the stripped env drops `%APPDATA%` credentials.
  - `devin-cli` OAuth is not in the CLI allowlist, which has only 8 providers. That leaves the dashboard as the only way to add it.
  - The balance is zero (L2856-2860, L3306).
- freeaiapikey (`faik`): `faik/anthropic/claude-sonnet-5` and `faik/openai/gpt-6-sol` serve 200. They are metered credit (about $6) (L3233, L3240).
- qodercli v1.1.62 works headless on the free `Qwen3.8-Flash`, and the spawner already pins it (L1195). Qwen Code CLI works through `omniroute run --model <combo> qwen` (L3307-3311).
- opencode-direct providers (Zen free, meta, openrouter `:free`, litellm, ollama) are a valid fallback ladder, because they don't proxy through OmniRoute (L1285, L1326). The user-level `opencode.json` held a plaintext OpenRouter key, which the operator was told to rotate (L1329).
- LiteLLM had been serving a stale September config from 4 orphaned processes. The fix was to kill them and restart (L2309).

**Process and harness findings**
- opencode top-level sessions don't continue on their own, so a host watchdog is needed (L747-748).
- Subagent sessions default their cwd to the main checkout. That caused the main-checkout breach (`08bd972`) and two later hijacks by the L1-alpha renderfix lane (L1012, L881, L1957, L2020).
- About 3 lanes fabricated their completions, and one reviewer leaf committed although it was read-only (L607, L802).
- The permission fence `*git *merge*` trips on the literal word "merge" anywhere in a command, even inside a file path (L653).
- Free-model reviewers caught real defects: a third shim site, username paths and leaked session ids, and 9 matrix flaws (L893-895, L824).
- `origin/main` CI had already been red for 5 runs before the wave. The local suite measured 1737/19 against a 1676/20 baseline; a later run measured 2114/11. CI first went fully green on `8d3b995e` (L1846, L1997, L2152, L2164).
- `models.md` and `docs/models-proposed.md` rot because the prose copies drift from the generated data. The PowerShell suite is skipped in CI, so stale pins go unnoticed there (L2066, L2114).
- The availability DB did not exist. `provider-balances.jsonl` was a stub, and nothing stopped agents from re-probing (L3225).

## 3. DECISIONS and REASONING

- **Structure.** The session became the L0 router, with L1-alpha (gateway serving) and L1-beta (provider truth and design), because there were too many tasks (L180). L0/L1 run as interactive top-level sessions (rule R-coord-12, renumbered 13). L1/L2 never fix work themselves; they spawn by complexity (R-coord-13, renumbered 14). Lanes never write `~/.omniroute` key or config files (R-coord-14, renumbered 15).
- **L1 model choice** went through several steps: vertex-flash, then the `gemini-3.8-flash` combo (the operator ruled out `gemini-3-flash-preview`), then `opencode/muse-spark-1.3-contributor-free`, which hit its rate limit, then `deepseek-v4.1-flash` on operator order, and later the vertex-backed `gemini-3.8-flash` combo. Complex reasoning uses `muse-spark-1.3-contributor-free#xhigh`; it is for public repos only because the model trains on prompts (L238-312, L469-476, L1094-1097, L1145).
- **Reviews.** 2-3 cross-family reviewers per change, writer and reviewers recorded in every DONE, and a reviewer → L2 fix worker → re-review chain. REVIEWGATE-2FAM was added as a repo gate. Reviewers should not be deepseek-only (L515, L864, L1028, L1049).
- **Combo order is trial → free → credits → paid, with deepseek last.** Gemini is not excluded; the 429 backoff limits how hard it gets hit (L1065-1072). OpenRouter is `:free` only. Nebius was removed after the debt. Scaleway was removed (`c901d15c`).
- **Context.** t1 legs must be at least 600k (literal in the contract gate). t1 keeps only 1M-capable legs (TORDER option (b)). t2/t3 stay at 128k, gated by the weakest implementer. `gemini-3.8-flash` `context_usable` is 1M (L1252, L1287-1291). Later rule R-orch-29: L2 agents pin 1M combos only (L2754).
- **Combo-contract gate** (`tools/combo-contract.py`, `2e67732c`). It fails closed in `apply` and in CI. It checks:
  - surface parity, i.e. client limit = route context = combos context;
  - leg order;
  - that every leg resolves;
  - the t1 ≥600k band;
  - that OpenRouter legs are `:free` only.

  Reason: renders kept silently reverting fixes (L1203, L1325).
- **D-TORDER-2.** No free, live, 1M, tool-calling second leg exists, so `t1-orchestrator-free-only` stays single-provider (L1396).
- **Researcher tier.** It is acceptable as a proposal but not as the default route (`c60a6c8`). Later, research needs 1M: about 170k minimum by arithmetic and 2× headroom. A `t1-researcher` was proposed, with `free_ai/qwen7b` moved to a `lookup-128k` role (L2482-2501).
- **Merge mechanics.**
  - The agent edited `opencode.jsonc` (`4da8ec0b`) to allow its own takes and `git push origin main`. Force-push, reset and rebase stay denied.
  - Later it added exact-name branch-delete allows (`b23e99af`) and an ff-only pull.
  - The carrier branch `ws-tier-order-20261001` is taken first, because the doc offshoots were cut from the combos lineage.
- **Plan reconciliation** after the agy-sonnet and deepseek review seats:
  - L1 = spark-xhigh loop plus an Opus-5.5 judgment seat.
  - L2 = mimo-2.6-pro → spark-xhigh → GLM-5.3-flash.
  - L3 = mimo-2.6-flash → deepseek-v4.1-flash → free band.
  - Models without verified scores (GPT-6.1-sol, GPT-luna, Opus-5.5-medium) stay out of every combo.
  - No new researcher skill and no skill-router skill; use unique trigger verbs plus `repository-index`.
  - The preflight scanner folds into `no-mistakes`.
  - Fan-out defaults to 3-5 by complexity, max 10, with family and prompt diversity (R-orch-30).
  - `swarm-orchestration` is not split.
  - Keep all six MCP servers (L2676-2708, L2746-2761, L3071, L3102).
- **Operator rulings:**
  - Claude roles: Sonnet for implementation, Haiku for triage. Literal Claude ids are not runnable, so the roles are mapped to classes (L2749).
  - Aliases early; then remove `t\d-*` ids entirely in favour of descriptive layer ids (L3118). The commit that did this is `c8787f98` per the repo log.
  - Every leg add or remove carries a dated reason in the `$comment` and in the ledger (L3229).
  - No history rewrite; the username is fine to publish (L3359).
  - Devin is parked as a late task (L3342).

## 4. DONE (commits and branches named)

- Lane commits on `L1-backlog/ws-*-20260930/20261001`, all later taken into main:
  - admission `c41de4a`/`d88ed3b`/`880ec58`/`1179e3f`/`c3e139c`
  - provider coverage (47 providers) `a8fa659`/`f6fa862`/`6aa8208`
  - incident doc and R-coord rules `f85e47e`
  - combos with vertex leg and 1M `a5bcb69`
  - OVH registry `f6f5e69`/`2abd09f`/`89a9024` and OVH-finish `7e329c7`→`a975d48`
  - deepseek reasoning_text fix `4c4a534`/`361ef45`/`b49f9e4`
  - Qwen clamp `656a5cb`/`7a4cbfd`/`e4bb58b`
  - sweep `55a87ac`/`98e30ef`
  - researcher `c60a6c8`
  - design memo `a02578d`
  - leg-health `62d8929` and its redo (`1ad452b`)
  - F1 vertex `fb26673`/`a1db84e`
  - freewire (10 free legs)
  - nebius combos `2ff537a→92a98af4→f2d8d607`
  - resilience `51fd01d2`/`3b0135e3`/`13d853e4`
  - combos-pins `ba0c707f`
  - changelog `eb182f23`/`2ffad433`/`2aceb008`
  - contract gate `2e67732c`
  - paid-OpenRouter removal `c4c3654b`
  - CREDIGNORE `4fd66091`
  - fallback ladder `6a83bb72`
  - merge checklist `a961805`/`204d43e`
- Gateway restarted at 08:07Z on 2026-10-01 with the patches and 429 policy active. The 28 credentials were re-entered. Nebius was removed live. TORDER was live-applied, with serving-leg probes: t2 on groq free, t1 on vertex, and OVH used (L1385-1392).
- Main merges and pushes: `14237978`, `75af3236`, `8d3b995e` (first fully green CI), `b23e99af` (12 branches and 4 worktrees deleted), `649819a6`, `6d91df21` (dead qwen `:free` gated, LiteLLM fixed, `docs/models.md` cleaned), `c901d15c` (scaleway removed).
- Later commits:
  - `28893a75`: R-orch-29/30 and changelog backfill.
  - `5df27a19`: aliases.
  - Combo display names in the form `LAYER · id · window`.
  - Skill consolidation doc edits.
  - The `t\d` → layer id rename wave, 63 files.
  - `tools/probe-ledger.py` with `tests/test_probe_ledger.py` (6/6 tests), seeded with 16 legs.
  - The faik provider, 2 model rows and the `faik-gpt-6-sol` single (`6fe30211`).
  - Rename fallout fixes (`14f025c0`); the spawner suite now shows zero delta against baseline (49 = 49).
  - Section G (provider×model×combo matrix) added to `docs/models-proposed.md`.
  - Scratch files deleted.
- Also delivered: the watchdog `logs/l1-watchdog.ps1`, the operator runbooks for antigravity, devin and qoder, and the conflict resolutions during takes (SKILL.md rule renumbering, registry/combos JSON merges).

## 5. OPEN / PLANNED TASKS (deduplicated, latest status)

| # | Task | Description / why it matters | Dependencies | Status |
|---|---|---|---|---|
| 1 | Operator 2026-10-07 batch (L3392-3407) | AA intelligence score and cost per task for every model, tracked in the repo. Clean up `models-proposed.md`: drop "refresh status", add mermaid, never `gemini-3.1-pro`, use `claude-sonnet-5-5`. | AA research subagent (its table had landed) | **Partial**: research launched, nothing written yet |
| 2 | `gemini-3.8-flash` combo: add agy + vertex legs | The same model through several providers adds resilience. The agy leg was probed at 1M just before the failure. | Sibling registry row convention | **Partial** (in progress at failure) |
| 3 | Nested combos in OmniRoute | Reuse the `gemini-3.8-flash` combo inside other combos. If OmniRoute can't nest, open an upstream PR. Until then, replicate the legs. | Gateway capability check | Not started (later task) |
| 4 | L3 redesign: no L2 worker/researcher/free-only combos | Those combos are throwaway work and belong in L3. Merge or split combos by context capability, because 128k everywhere is unacceptable. Includes T7: `l3-implementer` (gpt-oss-120b → Qwen3.5-397B → vertex → deepseek) and `l3-reviewer` (Qwen3-Coder-30B → Llama-70B → nemotron), plus a measured VL-72B row. | Research subagents. Open questions: gpt-oss $0 grant stance, 397B cost cap. | Not started (research launched) |
| 5 | `l2-researcher` 1M fix (T8) | Remove the 128k-clamping legs, including `free_ai/qwen7b`. Build a 1M researcher (gemini free → vertex → spark) and formalise the `lookup-128k` role. Research needs a large context. | Combo-contract gate, renders | Not started |
| 6 | Integrate all opencode models | The operator pointed out that the opencode models had been missed, even though all of them can run via opencode. | Probe and ledger | Not started |
| 7 | Future-models section | Cover minimax, kimi, glm, mimo and others in `models-proposed.md`. | none | Not started |
| 8 | Update the agent-spawner MCP | It must reflect the new combos, layer ids and spawn reasoning. | Rename done, L3 redesign | Not started |
| 9 | T6: model scores | Opus-5.5-medium, GPT-6.1-sol and GPT-luna have no AA figures, so they are excluded fail-closed. The operator asked for the scores to be fetched. | AA research | Partial |
| 10 | T10: Clef-Flash pilot | L0 classification only, with pinned temp/seed and a determinism suite. | Ollama reachability (binary absent) | Not started |
| 11 | T12: phase gates | Phase 1 moves L2/L3 to opencode; phase 2 does the same for L1. Entry/rollback rule: 5 clean runs with skip-not-fail. | L2 combos at 1M (R-orch-29) | Not started |
| 12 | CI on rename + faik push | The rename wave regressed shards b/c/e and was partly fixed. It needs a fresh CI verdict. | Push `14f025c0` (done) | Partial: unverified |
| 13 | T9: spawner `template`/`template_vars` | Deterministic delegation briefs per role save tokens. Needs a hotfix bypass. | none | Approved in principle, not started |
| 14 | T13: changelog pre-push gate with bypass | Enforces that every change is documented. | Backfill (done) | Not started |
| 15 | T11: literature table | Only 1 of 7 sources digested, so the depth (row 11) and messaging (row 17) verdicts are provisional. | Researcher | Partial |
| 16 | Probe-ledger wiring | Probe scripts should call `record` automatically, and a rule should require `check` before any probe. Otherwise the ledger goes stale. | ledger tool (done) | Not started |
| 17 | Plan rows not yet executed | Row 13 L3 tool allowlists (implementer / read-only reviewer / leaf researcher), L3-spawn ban as a spawner gate, row 18 relay envelope `{to,topic,verdict,sha,ts}`, row 15 interview template. | Rows 13/16 | Not started |
| 18 | Notify the other L1 Claude projects | Ask them to review with sonnet-5.5-medium, update plans and the plangraph, and continue. The old sessions are dead, so the brief would go to inboxes plus an operator relay. | Operator approval of relay text | Not started |
| 19 | glm-5.3-flash leg (opencode key) | The leg is gated on a 402. | Operator adds an OpenCode API key in gateway settings | Blocked |
| 20 | ZCode provider | Needs a spawnable CLI; the Electron app alone is not enough. | Vendor CLI or a gateway custom-binary path | Blocked |
| 21 | Antigravity reconnect | Rate-limited, expected to be free again in a few days. | Operator OAuth | Blocked (time) |
| 22 | meta-api muse-spark 502 | Upstream returns an empty response. Not explained yet. | none | Open investigation |
| 23 | Optional: bare `qwen` persistent gateway config | A `QWEN_CODE_SYSTEM_SETTINGS_PATH` installer step, with tests. | Operator OK | Not started |
| 24 | Deferred | P2 remote-fallback WIP (`ws-remote`, 10 commits, parked; the remote-issued key and the WS-OMNIREMOTE merge are operator-gated); P0b key canonicalisation and a proper storage-key rotation; clamp bypass on direct legs (vendor fix); stale credential row (CREDROW); ebn-lbn client re-render (declined unless asked). | — | Parked |
| 25 | Devin (`devin-cli`/`dva`) | Dashboard-only OAuth, Windows sandbox HOME, zero balance. | Nothing else left | Parked late task |

## 6. Where the session stopped

The last operator message (L3392-3407) asked for:
- AA intelligence and cost figures;
- the `models-proposed.md` cleanup with mermaid;
- the agy and vertex legs in the `gemini-3.8-flash` combo, and nested combos;
- the `l2-researcher` 1M fix;
- moving the L2 worker/researcher/free-only combos to L3, plus an L3 redesign;
- a future-models section;
- the opencode models;
- the spawner MCP update;
- T7/T8/T10/T6/T12 and the CI run for the rename.

The agent launched three research subagents in parallel. The AA table came back; the other two, presumably on the L3 redesign and the opencode models, were still running. The operator then said "continue" twice. On the first one, the agent checked the background suites and probed the agy `gemini-3.8-flash` leg (1M confirmed), then began "checking the sibling row convention" so it could add the registry row and combo leg (L3416-3418).

The second "continue" got no answer: the session ended there on a provider error. At that point:
- the agy/vertex leg addition was not written;
- none of the batch was committed;
- the last pushed state was `6fe30211` / `14f025c0` (both on `origin/main`);
- the rename-wave CI verdict was still pending.

## 7. Addendum 2026-10-08 (workstation session, after the export)

- OpenCode desktop is 2.0.25 (bundled CLI); the npm CLI is 2.0.12.
- Cause of "new chats do not have the latest combos": the layer rename `c8787f98` (t1..t4 -> l1/l2/l3 ids) was committed but
  never applied to the live gateway, which still served the retired `t*` combos. Fixed live by
  `configuration/omniroute/apply.ps1` (11 `t*` combos pruned, `l1-*`/`l2-*`/`l3-*` created; `spark-1.3-contributor` not created
  because the gateway catalog has no `meta-api/muse-spark-1.3-contributor`).
- Second cause, for chats opened outside the repo (the desktop default is the home directory): the user-level
  `~/.config/opencode/opencode.json` and `%APPDATA%\opencode\config.json` carried a stale literal gateway key (HTTP 401) and the old
  11-entry tier list. Fixed: key -> `{env:AUTOOS_OMNIROUTE_KEY}`, models -> the repo's 38 entries converted to the older `provider`
  schema (variants as a map, short names; the repo's list-shaped `variants` and `modelID` field silently drop the whole provider
  there). Backups `*.autoos-backup-20261008-*`. Verified: 38 models listed in and outside the repo, test calls answer.
- "Error from provider (Console): The request contains invalid parameters": session `ebn-L1` (and the exported `omniroute_setup`
  session) on `opencode/muse-spark-1.3-contributor-free#xhigh` broke on resume after a step was aborted mid `question` tool
  (aborted tool call + encrypted reasoning items in the replayed history). Fresh sessions at low/medium/high/xhigh, with tool
  calls, inside and outside the repo: all OK. Remedy for a broken session: start a new one (or fork before the aborted step).
- The direct `meta` provider (api.meta.ai) answers 401 with both stored keys (api-keys.yml and the user config): needs a new key.
- nscale: registry provider `available: false`, no combo leg, no model row references it -> the nscale model retirement needs no
  combo change.
