# L2 brief: build-L2-keeper-verbs — 5 keeper verb items (D-1542 backlog; HIGH: keeper engine; parent build-L1; TTL 6 h)
Rules: /home/s/code/plangraph-worktrees/build-L1/docs/plan/rethink/briefs/l2-rules-common.md (binding, incl. D-1380, PR-head trailer).
D-1533: writers + seats non-Claude first (mimo-v2.6-flash via mimo provider, qwen3.8-flash via qodercli, muse via opencode), Sonnet last (record why).
All 5 items claimed by build-L1 (epoch 1; build-L1 heartbeats). Repo plangraph, from origin/main (96426e67). Never merge, never touch the live
keeper (127.0.0.1:8787 read-only; no systemd). Done text: `ops/pg.sh show <key>` from /home/s/code/plangraph-worktrees/build-L1 (read-only).
One branch + draft PR per item, head trailer `Plan-Item: plangraph:<KEY>`, order:
1. PG-ROUTER-REOPEN (p5): router-only reopen(key, reason, decision_id): in_review -> claimed (same claimant, epoch+1), never closes/passes a review;
   next close still needs commit+ci + passing review at tier; hash-chained event; fold/verify agree; all doors (HTTP, CLI, MCP); tests.
2. PG-L1-CREATE-WITH-EDGE: create accepts `under: <gate/goal key>`, edge in same transaction only if caller may manage that project; forged
   cross-project + negative tests.
3. PG-ESTIMATE-SRC-EDIT: edit_definition accepts estimate_src with estimate_h; estimate_h edit without src records src = editing principal; fold/verify.
4. PG-LE-DEPS-OPS: apply_batch add_dep/remove_dep; full depends_on edit naming a cancelled item drops it with a warning.
5. PG-LE-GUARD-KEY: credential-guard refusal names item key + field + masked span; path-like strings (MT-003, MT-095) pass; real secrets refused.
New event types or schema change: check SQLite + PG parity (schema.py / schema_pg.py migration) and say so in MERGE-READY (live-sync impact).
Before each PR: existing suite for touched modules + new tests; full local suite once per PR before push; CI per common rules.
Seats: 2 non-Claude families PASS on the final head, HIGH seat format per vector file:function (auth/role checks, fold/verify parity, event
replay, negative paths). Seat dirs plangraph-worktrees/keeper-verbs-seats/<KEY>-<sha>/{qwen,mimo|openai}.
Per item MERGE-READY HIGH <head sha> + PR + CI run + seat dirs to build-L1; continue with the next item without waiting.
At the end: one line listing which items need a keeper live-sync (all likely) — build-L1 batches them into one LIVE-SYNC.
