# L2 brief: build-L2-ui-batch — 4 ready plangraph items, no live step (standard risk; parent build-L1; TTL 6 h)
Rules: /home/s/code/plangraph-worktrees/build-L1/docs/plan/rethink/briefs/l2-rules-common.md (binding, incl. D-1380, PR-head trailer, D-1554 'Writer:' trailer).
HOST coding.vm: D-437/D-1554 ONLY TOOL-LESS writers and seats (review-call pattern: prompt = spec + current file contents inline; L2 applies the diff).
Recipe: predecessor card /home/s/code/plangraph-worktrees/keeper-verbs-seats/L2-keeper-verbs-card.md "Writers = tool-less review-call" (mimo-v2.6-flash via
mimo provider, --max-tokens 40000). D-1504 model paths: qwen3.8-flash ONLY via qodercli; mimo via mimo provider; gemini-3.8-flash via agy/AI Studio. Sonnet last (record why).
D-1272: L2 may write <= ~10 changed lines itself (never auth/secrets), recorded as Writer: L2.
All 4 items claimed by build-L1 (epoch 1; build-L1 heartbeats). Repo plangraph from origin/main (418e6d7e or newer). Never merge, never write to the live keeper
(127.0.0.1:8787 read-only), no systemd. Own worktree under /home/s/code/plangraph-worktrees/ui-batch (never commit in /home/s/code/plangraph).
Done text: `ops/pg.sh show <key>` from /home/s/code/plangraph-worktrees/build-L1 (read-only). One branch + draft PR per item, head trailer `Plan-Item: plangraph:<KEY>`, order:
1. PG-UI-TABICON (1 h): favicon/tab icon, tab title no longer 'Plan graph'; render test.
2. UI-TABLE (12 h est; deps UI-TABLE-MODEL/PHONE/VIEW/WIRE are DONE, so most is built): first audit main against the done text, list gaps, close only the gaps.
3. PG-NEEDS-YOU: one Needs-you table replacing tab-needs + tab-qa (filters type/project/priority/age/state, sortable, deep links).
4. PG-L2-SMALLFIX-RULE (docs): D-1272 clause into plangraph L1/L2 brief templates + orchestration docs (text in the item's done criteria).
UI check (D-323): Playwright at 1440x900 and 390x844 against a LOCAL render of the static page with a fixture or a read-only snapshot copy (never the live
page as the system under test), screenshots to plangraph-worktrees/ui-batch-seats/<KEY>-<sha>/shots/, checklist run by a non-writer agent (seat).
Playwright RAM: one browser at a time; MemAvailable >= 1 GB before launch.
Seats: >= 1 countable non-Claude PASS on the final head (2 different families preferred), review.txt + evidence.json (finish_reason=stop, test_gaming=no) in
plangraph-worktrees/ui-batch-seats/<KEY>-<sha>/{qwen,mimo|openai}. A dismissed fix verdict is NOT countable; report "N countable + M dismissed".
No schema/auth/keeper-engine change in this batch: if an item needs one, STOP that item and send BLOCKED to build-L1 (it becomes HIGH).
Per item MERGE-READY <head sha> + PR + CI run + seat dirs to build-L1; continue with the next item without waiting. RECYCLE-READY <card> before 230k.
