# HOST-READY-WSL-HOP-FULL-PATH-AND-DOCTOR-SKIP

Outcome. Readiness and placement reach the workstation WSL guest: every WSL hop runs host tools under the registry entry's env.PATH, and a doctor report with skip rows reads as passing.

- plan_source. keeper card plangraph:HOST-READY-WSL-HOP-FULL-PATH-AND-DOCTOR-SKIP (Why/How/done), routing D-6967 and D-6968.
- base commit. d948affa1da9565f12c58fb981bbf2dbc04974ad
- card SHA-256. cee82508531c374199e64a234a55ce4a87eb03e1a20328e5f43fdbc6adcbc290
- tree SHA-256. 0fb4e40c1e0e0fb03c914a9dbb6268f8fc2a3d7753e77633759b80fc024921ce
- evidence_after_deploy. Live: on the orchestration host `python3 -m plangraph.spawner.host_ready --host ws-build` prints `ws-build ok` and writes the ws-build readiness record; pg-worker-liveness stays green.

## Decisions

- D1. No root change on the guest (routing D-6968): the fix is in the hop command, not a /usr/local/bin link.
- D2. PATH comes from the registry entry's env.PATH (already accepted by the Rust schema, rust/worker-core/src/hosts.rs::HostEntry.env).

## Entry points

- E1. src/plangraph/spawner/remote_ps.py::invocation and ::host_invocation (the `wsl.exe --distribution <d> --exec pg-spawnd steps` hop)
- E2. src/plangraph/spawner/host_ready.py::check_host and the parse_doctor reader in src/plangraph/spawner/host_ready_source.py
- E3. rust/worker-core/src/host_command.rs and its WSL caller rust/pg-spawnd/src/worker_liveness.rs::remote

## Acceptance

| Row | Entry | Observable result | Evidence |
| --- | --- | --- | --- |
| B1 | E1 | A WSL host entry with env.PATH yields a hop that sets that PATH before pg-spawnd steps, so the guest finds tools in ~/.local/bin without a login shell. | X1 |
| B2 | E2 | A doctor JSON whose rows include status skip parses; doctor_ok follows only fail rows. | X2 |
| I1 | E1 | A host entry without env.PATH keeps today's hop command byte for byte; an env.PATH value with shell syntax is refused. | X3 |
| B3 | E3 | The Rust WSL hop applies the same env.PATH rule where it runs guest tools. | X4 |

## Verification

- X1. E1, changed. pytest tests/test_spawner_remote_ps.py::test_wsl_hop_sets_registry_path (new). Asserts B1's observable result.
- X2. E2, changed. pytest tests/test_host_ready.py::test_doctor_skip_rows_parse_as_ok (new). Asserts B2's observable result.
- X3. E1, changed. pytest tests/test_spawner_remote_ps.py::test_wsl_hop_without_path_unchanged (new). Asserts I1's observable result.
- X4. E3, changed. cargo test -p pg-worker-core host_command::wsl_hop_sets_path (new). Asserts B3's observable result.

Anchors for new tests are predicted until checked on the implementation head.

## Scope and reuse

- R1. Reuse the existing hop and readiness code paths; no new host tool, no root change on any host.
- N1. No change to gate paths (.github/workflows, tools/ci_commit_gate.py, go_verify.rs, fast_checks*).

## Open points for design approval

None.
