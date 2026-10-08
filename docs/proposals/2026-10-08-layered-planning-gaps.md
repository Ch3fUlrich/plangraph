# Layered planning: operator spec vs. reality (D-639 step 5)

Spec: routing `docs/plan/operator-layered-planning-2026-10-08.md`. Checked against the skill (`skills/plangraph-planning`,
moving to AutoOS `layered-planning`, D-646), the spawner (`AutoOS/tools/autoos-agent.py` + MCP), docs, and the plangraph
keeper and its MCP server. Checked 2026-10-08 by plangraph-L1.

| # | Spec item | Where implemented | Missing |
|---|---|---|---|
| 1 | Hierarchy project → planning/work phases → L3 agents, expandable in the page | skill §1; page: project/phase clusters + Projects tree (collapse/expand) | keeper kinds are only lane/gate/goal; no `planning`/`work` phase kind, no `layer`/`role`/`agent_id` fields (the page guesses role from the title, page_model.js roleOf) → plangraph MODEL-KINDS |
| 2 | Model + effort per layer (Opus medium L1, Opus high planning, Sonnet high work) | skill §1 (tier aliases); spawner cards route by role/complexity | no per-layer effort field in the spawner card; the layer ids rename is pending (autoos:RS-SPAWNER-MCP) |
| 3 | 3–10 parallel research tasks, mixed families | skill §4; spawner `not_family` + FAMILYFENCE | no fan-out verb; parallelism is capped in practice: free legs = 1 per provider (`policy.free_concurrency`), opencode workers queue one at a time (seen today: 20-min queue timeouts) |
| 4 | Combine research → plan → 3 plan reviewers → refine | skill §4 | no tool support (manual L2 work); no record template for the 3 lenses |
| 5 | Work phase: optional detail research, refine, cheap implementer, parallel when independent | skill §4; spawner isolate = own clone per run | same parallelism cap as 3 |
| 6 | Spawner provides lint, tests, coding practices | skill §2 says so | spawner briefs carry no toolchain block (the author writes it by hand); no coding-principles injection |
| 7 | 1–3 reviewers, very cheap, other family than the implementer | spawner FAMILYFENCE, `review_of`; review-call.py for tool-less seats | writer family is often `assumed-default`/unresolved; not_family names differ between tools (openai-oss vs openai) |
| 8 | One task = one agent | spawner isolate + keeper claim/lease | no keeper link from item to spawner run id |
| 9 | A failed task/review spawns a follow-up task | skill §5 (`fixes` edge, max 2) | no `fixes` edge kind in the keeper; the spawner never creates a fix item; L1 does it by hand |
| 10 | Task size neither too small nor too big; operator chooses | skill §3 (10–40 min, ≤ 3 files), marked provisional (D-646 fixes) | the operator's choice (options below) |
| 11 | Compressed ERROR message on internal failure | skill §6 schema | spawner returns a raw output tail; keeper has no error-record attachment or evidence kind |
| 12 | Compressed DONE summary read by the phase planner; same between phase and project | skill §6 (done report, phase digest) | no schema check anywhere; spawner `result` is free text ("no-verdict" on reviews that do not end with VERDICT) |
| 13 | plangraph MCP server supports the flow | keeper MCP: ready/show/claim/heartbeat/submit/verdict/evidence/apply_ops… | no create/define for agents (router-only apply_ops), no digest/error-record tools, no layer/role filters |

## Task size: research (one cheap leg, muse-spark, 2026-10-08) + our own data

Fetched: METR 2025-03 (50% horizon ≈ 1 h for the then-best model, doubling ≈ 7 months; ~100% success under 4
human-minutes, < 10% over 4 h); Google small-CL guide (≈ 100 lines reasonable, 1,000 too big); Anthropic multi-agent
research (multi-agent uses ≈ 15x chat tokens). From model memory, not fetched: SWE-bench gold patches are tens of lines
in 1–2 files; SWE-Lancer top agent ≈ 26 %. Our data today (W1–W4, free legs): 3–6 files, 35–220 changed lines,
5–11 agent minutes each, 4/4 usable, 1 vacuous test caught by L1.

| Option | Limits per task | Expected first pass | Overhead |
|---|---|---|---|
| A Atomic | ≤ 2 files, ≤ 100 changed lines, ≤ 15k context tokens, ≤ 10 agent-min | 75–90 % | high per line (5–10k tokens of prompt + 1–3 min orchestration per task) |
| B PR-sized (recommended) | ≤ 3 files + tests, ≤ 200 changed lines, ≤ 30k context tokens, 15–30 human-min (10–20 agent-min) | ~60 %, ~80 %+ after one review/fix loop | moderate (15–30k tokens of context pack + 1–3 seat calls of ~5k) |
| C Feature | ≤ 10 files, ≤ 1,000 changed lines, ≤ 150k context tokens, ≤ 120 agent-min | 25–40 % | lowest per line, highest per merged change (retries, big reviews) |

Recommendation: B. One seat always; a second seat from another family for auth/money/data-loss diffs; a third only to
break ties. Split anything bigger on module or interface boundaries first.
