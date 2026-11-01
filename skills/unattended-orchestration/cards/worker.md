# Worker

Role: completes one closed task and returns a REPORT against its contract.

R-worker-01: Derive every render cell and test expectation from the registry render; pin only approved order; `--check` only, minimal edits. (why: `--out` erased 461 lines; source: 6a61052, f5d4e00)
R-worker-02: Author portable failing-first tests: seed bug state, assert the reason, guard platform; a dry run plans an absent tool. (why: the CI host lacks uv; source: CI 36227152085, 36360904338)
R-worker-03: Keep runs cheap: use `affected-tests.py`'s filter, full suite once per phase, snippet-lint,
  real `--no-cache` builds. (why: broad runs OOM or measure nothing; source: CI 108372206183, 1345e9b)
R-worker-04: Reproduce the real tool's observable contract in test fakes; read the real tool first, cite its lines. (why: fake drift hides real bugs; source: L1-backlog lstby 99742a1)
R-worker-10: Accept a detector or redactor on the real output corpus; give each new raw-data consumer its own redaction test. (why: fixtures passed; a secret leaked; source: SPAWNFIX3d, REDACTFIX3)
R-worker-11: Treat "summarise, no tools" as harness compaction unless a leading
  `<cross-session-message from=>` wrapper marks a peer. (why: transcript export; source: D-146, SB-C2, CompactionRuleTests)
