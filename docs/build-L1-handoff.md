CARD: /home/s/code/plangraph-worktrees/build-L1/status/build-L1.card.md (current state; read first)
# build-L1 handoff #7 (gen-7 -> gen-8, 2026-10-16 09:1xZ, ctx cap 230k)
Read first: brief /home/s/code/routing-worktrees/rethink-setup/docs/plan/rethink/build-L1-brief.md, card above.
Startup: skill unattended-orchestration (+ references/main-orchestrator.md); renew claimed leases FIRST (`sh ops/pg.sh heartbeat plangraph:<KEY>`):
CI-PYTEST-SPEED (lease ~10:2xZ), RT-P1-KEEPER-PG-PREP (10:49Z), CI-PG-CONTRACT-TIMEOUT (10:55Z). Recreate CronCreate beat `7-59/10 * * * *` (operator allowed).
Rules: D-1364 msgs only BLOCKED/DONE/MERGE-READY/GO/incident/launch. D-1390 effort medium, L2-LAUNCH "sonnet medium". D-1442: HIGH (keeper engine/live, schema, auth) -> judge server-L1-routing;
standard -> self-approve on non-Claude seats. Live steps need router GO (D-825). No GitHub billing; CI only workst/ci.vm self-hosted (fleet-ci). Card <= 40 lines.

## Gen-7 landed
KEEPER-LIVE-SYNC done (#26/#27 828e68e), NS-FLAKE (#25 ae5b749), ALARMS-READER HIGH (#28 8165c22, judge ACCEPT).
Live keeper = 8165c22 (GO D-1463, drop-in ~/.config/systemd/user/plangraph-keeper.service.d/live-sync-8165c22.conf over live-sync-1b923e5.conf;
backups ~/.local/share/plangraph/backups/live-sync-{1b923e5-20261016T064214Z,8165c22-20261016T082054Z}). ROLLBACK-HOLD worktree-proposal f3bac63 to 10-17 06:42Z.
Live-sync recipe that worked: new detached worktree .claude/worktrees/live-<sha> -> stop md-sync timer/path + render timer -> stop keeper -> no -wal
-> sqlite backup API copy + json + snapshot(+sig) + SHA256SUMS + counts (/home/s/.claude/jobs/2714142d/tmp/counts.py; recreate: per-table count JSON)
-> new drop-in (sorts after the old) -> daemon-reload -> start -> counts equal, admin verify/verify-snapshot (PG_CONFIG=keeper.json), /v1/show, /inbox -> timers back.

## In flight
- CI-PYTEST-SPEED: PR #30 head 998f8f3 judged PASS (my seat plangraph-worktrees/pytest-speed-seats/openai, dry-run rc0, writer anthropic).
  Blocker: dispatch 38875149323 pytest-postgres flaked; its rerun hung >20 min (runners likely saturated by pg-timeout load runs).
  Next: when green (or cancel + `gh workflow run tests.yml --ref lane/ci-pytest-speed`), `gh pr ready 30 && gh pr merge 30 --merge --match-head-commit 998f8f3...`,
  evidence commit+ci 998f8f3, submit, seat_verdict (no --dry-run), DONE to routing. Evidence needs: commit on main, trailer, newest run per job green.
- CI-PG-CONTRACT-TIMEOUT (D-1472): build-L2-pytest-speed (workst, session_017CG6p8ffUGmwwwZ1WXJJ3T) follow-on, brief briefs/l2-ci-pg-contract-timeout.md a5781e2; lane d09f870.
- RT-P1-KEEPER-PG-PREP (HIGH, D-1468): build-L2-pg-prep 47bd1f89 coding.vm, brief 5c0ec5b (trailer corrected to KEEPER-PG-PREP); lane b121d71. MERGE-READY HIGH -> judge.
- AO-DEEPINFRA-REMOVE / CI-RUNS-ON-WORKST: gated on workst-L1-autoos (owns fixes-L1 work, D-1455).

## Traps
Docs-only PRs skip tests.yml (paths filter): dispatch on a main-based trailer commit. Merge commits lack trailers: evidence = PR head sha.
Item closes at its tier's review count (2nd verdict may 409 "locked"). L2s self-declare tier wrong: check auth/DDL yourself (#28 was HIGH).
