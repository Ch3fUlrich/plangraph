# HOSTS-READY-TIMER-CODING-VM

Outcome. A host-ready timer on the orchestration host runs the readiness source for every registry host, so each has a fresh host-ready file.

- plan_source. keeper card plangraph:HOSTS-READY-TIMER-CODING-VM (Why/How/done), routing D-6968 and D-6972 (batch P).
- base commit. 06b885abcc7672087d5557e1120eec21233c4fd4
- card SHA-256. 86e9c3681488d46264e09ceafb448387534b320a2ee2df0fb00d05053f04ff39
- tree SHA-256. 39f708c8e33f15ed445b7217bed9249e5b3f3db23277cf6f9561c088d7716f05
- evidence_after_deploy. Live: host-ready files for dell and the workstation are fresh on the orchestration host after the deploy.

## Decisions

- D1. Interval shorter than the readiness record's freshness limit (600 s in host_ready.fresh). The timer waits 60 s after a sweep ends.

## Entry points

- E1. deploy/ unit + timer installed through the deploy manifest path
- E2. docs/host-tools.md readiness section

## Acceptance

| Row | Entry | Observable result | Evidence |
| --- | --- | --- | --- |
| B1 | E1 | The generated timer unit runs `python3 -m plangraph.spawner.host_ready` for each registry host and writes host-ready/<host>.json within the stale limit. | X1 |
| B2 | E2 | docs/host-tools.md names the unit, its interval and how to check it. | X2 |

## Verification

- X1. E1, changed. cargo test -p plangraph-cli --lib readiness_followup_unit_runs_readiness_for_registry_hosts. Asserts B1's observable result.
- X2. E2, changed. docs diff review. Asserts B2's observable result.

Anchors for new tests and files are predicted until checked on the implementation head.

## Scope and reuse

- R1. One implementation shared with the other keys of batch P; no root change on any host.
- N1. No gate path change (.github/workflows, tools/ci_commit_gate.py, go_verify.rs, fast_checks*).

## Open points for design approval

None.
