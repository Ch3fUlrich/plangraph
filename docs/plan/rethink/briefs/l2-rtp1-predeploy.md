# L2 brief: build-L2-rtp1-predeploy — RT-P1 WORKER, PROJECTOR, INBOX, REVIEW up to "only live Temporal missing" (parent build-L1; TTL 6 h)
Rules: /home/s/code/plangraph-worktrees/build-L1/docs/plan/rethink/briefs/l2-rules-common.md (binding, incl. D-1380, PR-head trailer).
D-1533 (operator order, binding): model-family agnostic — Claude is one leg; opencode server coding.vm :4096 is the fallback when Claude is rate limited. No hard Claude dependency in worker legs, seats or keeper clients; leg selection falls back to non-Claude on Claude rate limit (test it).
Host: WORKSTATION (Rust/Temporal builds; one CARGO_TARGET_DIR, cargo clean after). Repo plangraph from origin/main e70eb566.
Why: server:RT-P1-TEMPORAL-CHECK (live Temporal on cloud.vm) is blocked on operator certs until >= 02:30Z. Close every gap that does
NOT need that live cluster, so the four items only need the live run afterwards. Items stay unclaimed (deps open); build-L1 claims later.
Local Temporal = `temporal server start-dev` (or the SDK test server) on the workstation only — never the cloud.vm cluster, never 127.0.0.1:8787 writes.

## Phase 1 — gap matrix (no code; <= 1 h)
For each item: `ops/pg.sh show plangraph:RT-P1-<K>` from /home/s/code/plangraph-worktrees/build-L1 (read-only) -> done criteria; design
4.5/4.6 + section 7 (/home/s/code/routing-worktrees/rethink-setup/docs/reports/2026-10-15-rethink-setup-design.md); code on main
(rust/worker, src/plangraph/{projector,inbox,review}); prior lanes: briefs l2-worker-code.md, l2-projector-code.md, l2-inbox-code.md,
l2-review-code.md, l2-worker-proj-v1.md in build-L1/docs/plan/rethink/briefs + their PRs. Table per criterion: met (file:function + test) |
gap-local (fixable now) | gap-live (needs cloud Temporal). Write docs/plan/rtp1-predeploy-gaps.md on the lane; send build-L1 the
counts + the gap-local list in <= 15 lines, then continue (no wait) unless a gap needs schema/auth (HIGH) -> include and continue others.

## Phase 2 — close gap-local, order WORKER -> PROJECTOR -> INBOX -> REVIEW
One branch + draft PR per item: lane/rt-p1-<k>-predeploy, head trailer `Plan-Item: plangraph:RT-P1-<K>`. Writers per common rules
(D-1504 paths). Prefer an end-to-end local test: Rust worker + Card workflow on the dev server -> recorded history -> projector ->
keeper events (test keeper, SQLite/pytest-postgres) -> Inbox question/answer signal resumes the run -> reviewer activity verdict.
That e2e test is the main deliverable; it becomes the live-run script once TEMPORAL-CHECK lands.
Risk: projector/keeper-store changes = HIGH (2 non-Claude seats, judge via build-L1); worker/inbox/review-only = standard (1 non-Claude seat).
MERGE-READY per PR <head sha> + CI run (or D-1380 local record) + seat dirs (Taildrop + sha256 to coding.vm) to build-L1.
Also list per item the exact remaining live steps (gap-live) for build-L1's live-lane brief. Never merge, never live steps.
