# HOSTS-REGISTRY-WSL-LINUX-WORKER

Outcome. The registry generator and docs carry a Linux WSL worker entry (os linux, wsl_distro, ssh_target, caps, ramp, roles, env.PATH) and generate its overflow route with the WSL hop.

- plan_source. keeper card plangraph:HOSTS-REGISTRY-WSL-LINUX-WORKER (Why/How/done), routing D-6967 and D-6968.
- base commit. d948affa1da9565f12c58fb981bbf2dbc04974ad
- card SHA-256. c7f9e14eff1633d89bc63d5db5b0b38290d0785c465ba2a89090baa7d3cb7887
- tree SHA-256. 7c79072a00710c51c0d0b2317a622f4eb81750dfd899dd4e6aa6608b5a13729c
- evidence_after_deploy. Live: the orchestration host's hosts.json ws-build entry (applied by plan-order-0941 2026-11-26T14:25Z, D-6967) validates with pg-package-host and generates the route.

## Decisions

- D1. The live registry entry was applied by hand on the orchestration host under D-6967; this item adds the generator, test and doc side.
- D2. remote_placement is true for the workstation per operator D-6967, superseding the card's 'keep remote_placement off'.

## Entry points

- E1. src/plangraph/fleet_sync/genconfig.py::generate (wsl_distro branch, lines 55-70)
- E2. docs/placement-rules.md (worker host entries, D-5188)

## Acceptance

| Row | Entry | Observable result | Evidence |
| --- | --- | --- | --- |
| B1 | E1 | A registry with a WSL Linux entry (wsl_distro, ssh_target, remote_placement) generates an overflow route with roles, caps, the wsl hop and env.PATH. | X1 |
| I1 | E1 | remote_placement false on that entry refuses placement (REMOTE_PLACEMENT_CLOSED). | X2 |
| B2 | E2 | The docs show the WSL entry fields; the item's 'keep remote_placement off' is replaced by operator D-6967 (workstation placed first). | X3 |

## Verification

- X1. E1, changed. pytest tests/test_fleet_sync.py::test_wsl_linux_entry_generates_overflow_route (new). Asserts B1's observable result.
- X2. E1, changed. pytest tests/test_fleet_sync.py::test_wsl_entry_remote_placement_false_refused (new). Asserts I1's observable result.
- X3. E2, changed. docs diff review. Asserts B2's observable result.

Anchors for new tests are predicted until checked on the implementation head.

## Scope and reuse

- R1. Reuse the existing hop and readiness code paths; no new host tool, no root change on any host.
- N1. No change to gate paths (.github/workflows, tools/ci_commit_gate.py, go_verify.rs, fast_checks*).

## Open points for design approval

None.
