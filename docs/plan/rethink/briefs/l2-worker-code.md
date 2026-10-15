# L2 brief: build-L2-worker-code — plangraph:RT-P1-WORKER-CODE (HIGH; runs on the WORKSTATION; parent build-L1)
Rules: /home/s/code/plangraph-worktrees/build-L1/docs/plan/rethink/briefs/l2-rules-common.md (binding). TTL 6 h.
Done means (keeper item): Temporal workflow per card attempt over rust/worker-core (worktree, checks, confinement, probe, exit
classes, budgets, janitor); opencode harness adapter; advisory-lock merge with base recheck; worker build ids; worker-core R2
carry-over (rust/worker-core/REVIEW-R2.md last section: fresh worktree on promotion, 24 h wait cap -> park, probe_fail attempt cap,
symlink escape via sandbox); keeper glue behind a trait with an HTTP-contract stub (no live keeper); workflow replay tests + an
end-to-end test against a local `temporal server start-dev` in CI; 2 seats incl. non-Claude.
Inputs: design §4.5/4.6/14a /home/s/code/routing-worktrees/rethink-setup/docs/reports/2026-10-15-rethink-setup-design.md;
spike crate + findings on main (TEMPORAL-SPIKE, SDK 1.1.0, adopt-with-workarounds); rust/worker-core on main dd6300cf.
Repo: plangraph clone on the workstation, branch lane/rt-p1-worker-code from origin/main; draft PR for CI. CI must install the
temporal CLI dev server in the job (no live infra). Phases: W1 workflow+activities+adapter, W2 merge lock+build ids+carry-over, seats.
