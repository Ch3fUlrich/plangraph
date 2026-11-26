# HOSTS-READY-TIMER-CODING-VM

Outcome. A host-ready timer on the orchestration host runs the readiness source for every registry host, so each has a fresh host-ready file.

- plan_source. keeper card plangraph:HOSTS-READY-TIMER-CODING-VM (Why/How/done), routing D-6968 and D-6972 (batch P).
- base commit. 06b885abcc7672087d5557e1120eec21233c4fd4
- card SHA-256. ae0d6c384267506a33a9f4ef04f945299e5423a5981dd85b2c9e2a7a447d9c9d
- tree SHA-256. 94f81c5961912a8e6c58b2a28af08c442e732a75057ef5e4159bbac61c876964
- evidence_after_deploy. Live: host-ready files for dell and the workstation are fresh on the orchestration host after the deploy.

## Decisions

- D1. Interval shorter than the readiness record's freshness limit (record expires after 120 s per worskt-spawner's report); the writer reads the limit from host_ready.fresh.

## Entry points

- E1. deploy/ unit + timer installed through the deploy manifest path
- E2. docs/host-tools.md readiness section

## Acceptance

| Row | Entry | Observable result | Evidence |
| --- | --- | --- | --- |
| B1 | E1 | The generated timer unit runs `python3 -m plangraph.spawner.host_ready` for each registry host and writes host-ready/<host>.json within the stale limit. | X1 |
| B2 | E2 | docs/host-tools.md names the unit, its interval and how to check it. | X2 |

## Verification

- X1. E1, changed. pytest tests/test_host_ready_unit.py::test_unit_runs_readiness_for_registry_hosts (new). Asserts B1's observable result.
- X2. E2, changed. docs diff review. Asserts B2's observable result.

Anchors for new tests and files are predicted until checked on the implementation head.

## Scope and reuse

- R1. One implementation shared with the other keys of batch P; no root change on any host.
- N1. No gate path change (.github/workflows, tools/ci_commit_gate.py, go_verify.rs, fast_checks*).

## Open points for design approval

None.
