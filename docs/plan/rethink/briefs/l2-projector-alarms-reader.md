# L2 brief: build-L2-alarms-reader — plangraph:PROJECTOR-ALARMS-READER (D-1433 card 5c, prio 3, standard; parent build-L1)
Rules: /home/s/code/plangraph-worktrees/build-L1/docs/plan/rethink/briefs/l2-rules-common.md (binding, incl. D-1380, PR-head trailer). TTL 2 h. Never merge, no crons (D-1377 binds L2s). Effort medium (D-1390).
Gap: PROJECTOR-RECONCILE-SCHED (main cf9fc7b) writes projector_alarms (src/plangraph/projector/reconcile.py:36/118/124; DDL projector/ddl.py:38, open index = cleared_at IS NULL), nothing reads them.
Done means (keeper item): the Inbox (src/plangraph/inbox/routes.py + form.py; contract docs/contract/keeper-contract-v1-inbox.md) reads projector_alarms and shows each
open (cleared_at IS NULL) alarm with key, kind, workflow_id, age; "shown once" = an ack path (POST, keeper role check like the existing inbox writes) that marks it acknowledged
so it is not re-shown, cleared alarms never shown; no alarm silent (an unreadable table / query error renders an error banner, never an empty list).
Tests (failing first): fake alarm row(s) in a temp sqlite keeper db -> GET /inbox lists them; ack -> gone on next GET; cleared row never listed; error path banner. Run on sqlite; pg path only if the reader SQL differs (keep it portable).
Write set: src/plangraph/inbox/*, its tests, the inbox contract doc, ONE additive DDL column/table for ack state if needed (IF NOT EXISTS, sqlite + pg ddl both; live keeper now runs main 1b923e5 -> any new DDL must be idempotent in open_db; say so in MERGE-READY). Not reconcile.py's raise/clear logic.
Seat: 1 cross-family non-Claude seat PASS (gpt-oss via ops/seat.sh or qwen via qodercli) with V1..Vn answer format, seat_verdict dry-run rc 0, orphan branch seats/alarms-reader-<sha>.
Writers: free first (muse-spark free / OpenRouter free; mimo if 402 cleared), Sonnet last. Worktree from origin/main (ae5b749+), branch lane/projector-alarms-reader;
draft PR; head trailer `Plan-Item: plangraph:PROJECTOR-ALARMS-READER`; CI on workst/ci.vm only (no GitHub-hosted). MERGE-READY <sha> + CI run + seat branch to build-L1.
