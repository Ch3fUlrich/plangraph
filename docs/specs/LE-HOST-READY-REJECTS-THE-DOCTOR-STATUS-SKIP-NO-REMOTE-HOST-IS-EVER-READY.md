# LE-HOST-READY-REJECTS-THE-DOCTOR-STATUS-SKIP-NO-REMOTE-HOST-IS-EVER-READY

Outcome. A readiness record accepts every status the doctor emits; the parser and the doctor share one status list, so a healthy remote host reads ready.

- plan_source. keeper card plangraph:LE-HOST-READY-REJECTS-THE-DOCTOR-STATUS-SKIP-NO-REMOTE-HOST-IS-EVER-READY (Why/How/done), routing D-6968 and D-6972 (batch P).
- base commit. 06b885abcc7672087d5557e1120eec21233c4fd4
- card SHA-256. 5c1aacc6385e44dbbfebbd7e314d3a79cc7da498b3633d55f52ab2fc5dcab87a
- tree SHA-256. 6f73f8fac05703a8d4683ac310e62a30b7ec67f08b7ba629377ae002f8af941b
- evidence_after_deploy. Live: host-ready/ws-build.json on the orchestration host reads ok true and fresh, and one review seat is placed on ws-build by the spawner (run id).

## Decisions

- D1. Same fix as part (2) of HOST-READY-WSL-HOP-FULL-PATH-AND-DOCTOR-SKIP in this batch (routing D-6972); one implementation.

## Entry points

- E1. src/plangraph/spawner/host_ready_source.py parse_doctor (status list) and the doctor's status constants

## Acceptance

| Row | Entry | Observable result | Evidence |
| --- | --- | --- | --- |
| B1 | E1 | A doctor result with a skip check and ok true gives a readiness record with ok true. | X1 |
| I1 | E1 | The parser's accepted statuses come from the doctor's own list; an unknown status still fails. | X2 |

## Verification

- X1. E1, changed. pytest tests/test_host_ready.py::test_doctor_skip_rows_parse_as_ok (new). Asserts B1's observable result.
- X2. E1, changed. pytest tests/test_host_ready.py::test_doctor_status_list_shared (new). Asserts I1's observable result.

Anchors for new tests and files are predicted until checked on the implementation head.

## Scope and reuse

- R1. One implementation shared with the other keys of batch P; no root change on any host.
- N1. No gate path change (.github/workflows, tools/ci_commit_gate.py, go_verify.rs, fast_checks*).

## Open points for design approval

None.
