# Workflow versioning operations
Local launch: `DEVSTACK_DEPLOYMENT=d DEVSTACK_BUILD_ID=v1 tools/devstack.sh worker`
exports the same identity used by `set-current`; `PG_WORKER_BIN` overrides the binary.
1. Set explicit valid `PG_DEPLOYMENT` and unique `PG_BUILD_ID`; missing identity
   or `PG_VERSIONING=off` refuses startup before connecting. Run `tools/devstack.sh
   start` to start Temporal and Postgres (initial promotion fails until a poller
   registers). Start the worker in another terminal, then run `tools/devstack.sh
   set-current`; retry `start` to verify promotion succeeds.
2. Every command change requires stable `ctx.patched` or a new workflow version
   plus drain note. Never rename markers. Drain note: retain old pollers until
   pinned runs complete; never redirect incompatible histories to empty queues.
3. `bounded-runs-v1` preserves old replay. New runs roll over between attempts
   at 2,000 events, 100 waits/run, or server suggestion; retain total budgets,
   routing, history, base and answers. Attempts have total schedule-to-close.
4. Release manifests pin current `rust/worker/tests/fixtures/` bytes by SHA-256
   and source commit, without duplicate copies. Before changing a pinned fixture,
   keep its old bytes at `releases/<release>/<name>` (copy-on-change). Pin tests
   reject re-recording without that copy; replay runs every kept release copy.
   Record only into fresh `PG_RECORD_DIR/<PG_BUILD_ID>`;
   never remove old evidence. Run `cargo test --test replay` and
   `python3 tests/test_workflow_archive.py <merge-base>` to reject archive changes.
5. Run `cargo test` in `rust/worker` with `PG_TEMPORAL_CLI` pointing to Temporal;
   tests only: `PG_JAIL=off PG_TEST_ALLOW_UNJAILED=1`. Private-server tests prove
   SIGKILL at attempt/timer, replacement build, rollover caps and retry timeout.
6. Gate-Edit: `.github/workflows/tests.yml` archive rejection, workflow-change
   patch/version-plus-drain policy, subprocess job and required-CI dependencies.
