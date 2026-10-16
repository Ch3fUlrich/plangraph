# L2 brief: build-L2-planner-kit — plangraph:RT-P1-PLANNER-KIT (standard; coding.vm; parent build-L1)
Rules: /home/s/code/plangraph-worktrees/build-L1/docs/plan/rethink/briefs/l2-rules-common.md (binding). TTL 4 h. Effort medium.
Model paths (D-1504, no substitution): qwen3.8-flash ONLY via qodercli; mimo-v2.6-flash ONLY via the mimo provider
(omniroute/mimo-v2.6-flash pinned combo or ops/seat.sh mimo/mimo-v2.6-flash); muse-spark-1.3 ONLY via opencode; gemini-3.8-flash ONLY via agy
(429 -> back off or BLOCKED, never swap).
Already on main (RT-P1-PLANNER-KIT-CODE done): src/plangraph/planner/{card,readiness,routing,legs,context_pack,service}.py, docs/PLANNER-KIT.md.
Remaining (docs/PLANNER-KIT.md "Not here" + keeper done text), three closed L3 tasks:
 T1 readiness: leg max request — the gate refuses a card whose estimate/pack exceeds the routed leg's `max_request_tokens` (legs.py:41),
    reason string + planner-queue path; tests in tests/test_planner_readiness.py.
 T2 Jev shadow hook (design D18, §4.3 line ~113): optional shadow answerer beside routing.decide(): logs rules answer + shadow answer
    + later outcome (attempt result) as a JSONL/keeper audit row; shadow never changes the decision; off by default (env/config);
    failure of the shadow = logged, ignored. New module src/plangraph/planner/shadow.py + tests. No live model calls in tests (fake).
 T3 planning skill rewrite: skills/plangraph-planning/SKILL.md emits cards in the card.py schema (batch YAML example that imports via
    service), readiness + routing explained by pointer to docs/PLANNER-KIT.md; < 500 lines total incl. references; keep frontmatter.
Write set: src/plangraph/planner/{readiness,shadow}.py, tests/test_planner_*.py, skills/plangraph-planning/**, docs/PLANNER-KIT.md.
store*.py/schema*.py/migrations -> ask build-L1 first (keeper live on Postgres since 2026-10-16 16:43Z; schema = HIGH).
Done means: T1-T3 merged-ready on one lane, `python3 -m pytest -q tests/test_planner_*.py tests/test_split_make_public.py` green on
SQLite and the Postgres CI job; 1 non-Claude seat PASS (qwen via qodercli) on the final head; PR head commit carries
`Plan-Item: plangraph:RT-P1-PLANNER-KIT`.
Worktree /home/s/code/plangraph-worktrees/planner-kit, branch lane/rt-p1-planner-kit from origin/main; draft PR into main;
MERGE-READY <sha> + seat dir to build-L1. Never merge, never write 127.0.0.1:8787 except via your own claimed keeper token if given.
