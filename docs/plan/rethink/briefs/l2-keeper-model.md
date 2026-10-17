# L2 brief: build-L2-keeper-model — 3 ready plangraph keeper items (HIGH: plan-graph engine / CI verifier; parent build-L1; TTL 6 h)
Rules: /home/s/code/plangraph-worktrees/build-L1/docs/plan/rethink/briefs/l2-rules-common.md (binding, incl. D-1380, PR-head trailer, D-1554 'Writer:' trailer).
HOST coding.vm: D-437/D-1554 ONLY TOOL-LESS writers and seats (review-call recipe: /home/s/code/plangraph-worktrees/keeper-verbs-seats/L2-keeper-verbs-card.md).
D-1504 model paths: qwen3.8-flash ONLY via qodercli; mimo-v2.6 via mimo provider; gpt-oss via ovh. Sonnet last (record why). D-1272: L2 <= ~10 lines itself.
Items claimed by build-L1 (epoch 1; build-L1 heartbeats). Repo plangraph from origin/main. Never merge, never write to the live keeper (127.0.0.1:8787
read-only), no systemd. Own worktree /home/s/code/plangraph-worktrees/keeper-model. Done text: `ops/pg.sh show <key>` from build-L1 (read-only).
FIRST audit main against each done text (many parts may exist); report gaps in your first message to build-L1, then close only the gaps.
One branch + draft PR per item, head trailer `Plan-Item: plangraph:<KEY>`, order:
1. MODEL-LOOP: loop kind validated (exactly one exit decision, no orphan); views + validator handle it; tests.
2. MODEL-NAMES: description in schema/API/export; validator WARNING for id-like titles / missing description. The live backfill is a LIVE step: write it as
   a dry-run script + list only, never apply.
3. PG-CI-FORGEJO: Forgejo commit-status adapter (read-only token FILE, never in logs/argv, timeouts/caps, hermetic tests with a fake that matches the real
   API, cite the Forgejo API doc lines); keeper.json ci per project as a PROPOSED config diff in the PR (live config = live step). Token mint is not yours.
New event types or schema change: SQLite + PG parity (schema.py / schema_pg.py migration) and say so in MERGE-READY (live-sync impact).
Seats: 2 FRESH non-Claude families PASS on the final head full diff, per-vector file:function; review.txt + evidence.json (finish_reason=stop,
test_gaming=no) in plangraph-worktrees/keeper-model-seats/<KEY>-<sha>/{qwen,openai}. Dismissed fix verdicts are NOT countable: "N countable + M dismissed".
Per item MERGE-READY HIGH <head sha> + PR + CI run + seat dirs to build-L1; continue without waiting. End: one line on live-sync impact per item.
RECYCLE-READY <card> before 230k.
