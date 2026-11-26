# LE-WSL-EXEC-COMMAND-FINDS-NO-PG-SPAWND-AND-NO-PLANGRAPH-ENV

Outcome. The remote command for a WSL host finds pg-spawnd and the other host tools and carries the plangraph environment without a hand-installed wrapper; generated host configs never go stale.

- plan_source. keeper card plangraph:LE-WSL-EXEC-COMMAND-FINDS-NO-PG-SPAWND-AND-NO-PLANGRAPH-ENV (Why/How/done), routing D-6968 and D-6972 (batch P).
- base commit. 06b885abcc7672087d5557e1120eec21233c4fd4
- card SHA-256. b51854d4c20ecc45f7fd9865971bbd0c864a9d28ad39d7c6dfb65d7e83e17b68
- tree SHA-256. 5a35be4f2ea518b380cc3f96667badda9bf6c6ed5ba2e75f6ffca500785dbf49
- evidence_after_deploy. Live: the steps probe of ws-build from the orchestration host returns reachable ok; with the hop fix the /usr/local/bin/pg-spawnd link (operator-approved stopgap, 2026-11-26) is no longer needed.

## Decisions

- D1. One hop implementation with HOST-READY-WSL-HOP-FULL-PATH-AND-DOCTOR-SKIP part (1) (routing D-6972).
- D2. The token stays a file path; no secret value in any generated file.

## Entry points

- E1. src/plangraph/spawner/remote_ps.py::invocation (WSL hop)
- E2. src/plangraph/fleet_sync/genconfig.py::generate (ps-hosts.json, overflow-hosts.json)

## Acceptance

| Row | Entry | Observable result | Evidence |
| --- | --- | --- | --- |
| B1 | E1 | The generated remote command succeeds in a shell whose PATH lacks ~/.local/bin and whose environment lacks the plangraph variables (tools by the registry's env.PATH or path field; the remote side loads the host's plangraph.env, token only as a file path). | X1 |
| B2 | E2 | genconfig output equals the registry after an install; the stale ps-hosts.json win_src entry for ws-build is replaced by the WSL entry. | X2 |

## Verification

- X1. E1, changed. pytest tests/test_spawner_remote_ps.py::test_wsl_hop_without_login_path_finds_tools (new). Asserts B1's observable result.
- X2. E2, changed. pytest tests/test_fleet_sync.py::test_ps_hosts_regenerated_from_registry (new). Asserts B2's observable result.

Anchors for new tests and files are predicted until checked on the implementation head.

## Scope and reuse

- R1. One implementation shared with the other keys of batch P; no root change on any host.
- N1. No gate path change (.github/workflows, tools/ci_commit_gate.py, go_verify.rs, fast_checks*).

## Open points for design approval

None.
