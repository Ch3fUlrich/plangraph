# L2 brief: build-L2-reconcile — plangraph:PROJECTOR-RECONCILE-SCHED (standard; WORKSTATION per routing 04:1xZ; parent build-L1)
Rules: /home/s/code/plangraph-worktrees/build-L1/docs/plan/rethink/briefs/l2-rules-common.md (binding, incl. D-1380, PR-head trailer). TTL 2 h.
Done means (keeper item): a systemd service + timer in deploy/systemd/ runs `python -m plangraph.projector reconcile` nightly on the
keeper host (no agent cron, D-1377); install/enable docs next to the existing units; a reconcile mismatch raises a row in
projector_alarms that the Inbox reads (test; reuse src/plangraph/projector/reconcile.py tests if they already cover it, add the
unit-file lint/parse test); docs/projector.md "Operations" names the timer. 1 cross-family seat PASS. No live install (deploy is
server/prox under GO; this lane ships files + docs only).
Write set: deploy/systemd/*, docs/projector.md (Operations), tests. Affected tests only (test policy), CI on workst.
Worktree from origin/main, branch lane/projector-reconcile-sched; draft PR; head trailer `Plan-Item: plangraph:PROJECTOR-RECONCILE-SCHED`;
MERGE-READY <sha> + CI run + seat dir (durable path) to build-L1.
