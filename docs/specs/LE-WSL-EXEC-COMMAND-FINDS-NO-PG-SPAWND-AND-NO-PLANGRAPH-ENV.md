# LE-WSL-EXEC-COMMAND-FINDS-NO-PG-SPAWND-AND-NO-PLANGRAPH-ENV

Outcome. The remote command for a WSL host finds pg-spawnd and the other host tools and carries the plangraph environment without a hand-installed wrapper; generated host configs never go stale.

- plan_source. keeper card plangraph:LE-WSL-EXEC-COMMAND-FINDS-NO-PG-SPAWND-AND-NO-PLANGRAPH-ENV (Why/How/done), routing D-6968 and D-6972 (batch P).
- base commit. 06b885abcc7672087d5557e1120eec21233c4fd4
- card SHA-256. cb0c0626686f5b4a57638bc66e705df9a876837c5efcd377de4d95060fa7f074
- tree SHA-256. 6f83214c4aebafd9818b174f66457468185cf5a0dee1f839900e1286f5ebb232
- evidence_after_deploy. Live: the steps probe of ws-build from the orchestration host returns reachable ok; with the hop fix the /usr/local/bin/pg-spawnd link (operator-approved stopgap, 2026-11-26) is no longer needed.

## Decisions

- D1. One hop implementation with HOST-READY-WSL-HOP-FULL-PATH-AND-DOCTOR-SKIP part (1) (routing D-6972).
- D2. The token stays a file path; no secret value in any generated file.

## Entry points

- E1. src/plangraph/spawner/remote_ps.py::invocation (WSL hop) and Rust step runner, source probe and launch callers
- E2. src/plangraph/fleet_sync/genconfig.py::generate (ps-hosts.json, overflow-hosts.json), invoked by Rust host-sync release installation

## Acceptance

| Row | Entry | Observable result | Evidence |
| --- | --- | --- | --- |
| B1 | E1 | The generated remote command succeeds in a shell whose PATH lacks ~/.local/bin and whose environment lacks the plangraph variables (tools by the registry's env.PATH or path field; the remote side loads the host's plangraph.env, token only as a file path). | X1 |
| B2 | E2 | genconfig output equals the registry after an install; the stale ps-hosts.json win_src entry for ws-build is replaced by the WSL entry. | X2 |

## Verification

- X1. E1, changed. Run `test_wsl_hop_without_login_path_finds_tools` in `test_spawner_remote_ps.py` and `readiness_followup_steps_supply_guest_defaults_without_replacing_process_values` in the Rust `steps_cli` suite. These tests cover B1.
- X2. E2, changed. pytest tests/test_fleet_sync.py::test_ps_hosts_regenerated_from_registry and the Rust host_sync_release integration test. Assert B2's observable result.

Anchors for new tests and files are predicted until checked on the implementation head.

## Scope and reuse

- R1. One implementation shared with the other keys of batch P; no root change on any host.
- N1. No gate path change (.github/workflows, tools/ci_commit_gate.py, go_verify.rs, fast_checks*).

## Open points for design approval

None.
