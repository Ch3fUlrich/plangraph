# L2 brief: build-L2-deepinfra — autoos:AO-DEEPINFRA-REMOVE (standard; coding.vm; parent build-L1; AutoOS merger = fixes-L1)
Rules: /home/s/code/plangraph-worktrees/build-L1/docs/plan/rethink/briefs/l2-rules-common.md (binding; repo here is AutoOS, so the
plangraph-specific lines — keeper paths, split-public test, Postgres CI — do not apply; AutoOS gates do). TTL 3 h. Skill unattended-orchestration
rules R-orch-11 (grep every consumer of a removed id), R-orch-18 (registry verify = tests/test_registry_render.py + renderer --checks), R-coord-12 (tools/prepush.py green before push).
Operator (2026-10-15 ~20:3xZ, verbatim): "Mimo works only via the mimo provider route. Deepinfra models should be removed fully and disabled from omniroute."
Done means: no deepinfra model/provider left in catalog/ai-registry.json (29 hits at 3df719255), tools/registry.py provider maps
(~3469, ~3494 docstrings/logic), routes/combos, tests (tests/test_deepinfra_wire_min.py, test_registry.py, test_autoos_spawner.py,
test_t1_credit_fix10.py hits adjusted or removed with reason); spawner writer leg 1 = opencode `omniroute/mimo-v2.6-flash` (gateway
mimo/mimo-v2.6-flash) per D-1351 order mimo -> opencode free -> OpenRouter free -> qodercli, Sonnet only as recorded fallback;
`python3 tools/autoos-agent.py route --explain` on a writer card shows that order; `git grep -i deepinfra` = 0 except CHANGELOG/history docs.
opencode.jsonc already clean on main. Live OmniRoute has no deepinfra connection (fixes-L1) — no live step.
Worktree /home/s/code/AutoOS-worktrees/deepinfra-remove, branch lane/ao-deepinfra-remove from origin/main AFTER priv-writers merged
(build-L1 tells you the base sha). Writers per common rules (free first); 1 cross-family seat. Never ff /home/s/code/AutoOS, never restart
MCP (fixes-L1 does step 4). Report MERGE-READY <sha> + seat dir to build-L1 (I judge, then hand to fixes-L1 to merge).
