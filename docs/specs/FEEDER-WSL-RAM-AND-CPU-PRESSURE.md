# FEEDER-WSL-RAM-AND-CPU-PRESSURE

Outcome. Admit WSL writers using effective Windows and WSL free memory and refuse new writers above the CPU pressure threshold.

- plan_source. Keeper card FEEDER-WSL-RAM-AND-CPU-PRESSURE and D-6826, slice 2 of the FEEDER-LIVE-ALL-HOSTS replan.
- base commit. d948affa1da9565f12c58fb981bbf2dbc04974ad
- implementation commit. d900963a3
- card SHA-256. fb3ff67f4e7d538cc82c8e93df4d7318a5ec3202b4af5e644e62d9e3de9a7692
- tree SHA-256. e986ea38d3203e97165175213442aa8c2b6b34b3627e30f8e0f071396f65b3a6
- evidence_after_deploy. Deploy the rebuilt CLI and policy to approved hosts. Verify the Windows RAM reading in a dry run before enabling the WSL timer.

## Decisions

- D1. WSL MemAvailable can overstate the memory that Windows can supply. Sustained CPU pressure is an independent admission constraint.
- D2. Cap WSL MemAvailable at Windows free memory minus the configured Windows floor. Bound the Windows read and reuse a cached value for one facts window after a failure. Dry runs only read the cache. Read PSI some avg300 where available and apply the existing load fallback when PSI is absent.

## Entry points

- E1. Live resource sampling in rust/plangraph-cli/src/feeder/capacity.rs and windows_ram.rs.
- E2. Feeder admit and the runtime capacity decision.

## Acceptance

| Row | Entry | Observable result | Evidence |
| --- | --- | --- | --- |
| B1 | E1 | WSL admission uses the lower RAM view. A failed Windows read uses a fresh cache or refuses as windows_ram_unreadable. | X1 |
| B2 | E2 | PSI above the policy threshold refuses with the measured value and threshold. At the threshold, other resources decide. | X2 |
| B3 | E2 | The decision reports both RAM views, the Windows reading source and the PSI or load fallback source. | X3 |

## Verification

- X1. windows_ram tests and live fixture tests on_wsl_the_windows_free_memory_less_its_floor_caps_the_sample and an_unreadable_windows_side_admits_nothing_and_says_why.
- X2. cpu_pressure_refuses_above_the_limit_and_admits_at_the_limit and psi_text_gives_some_avg300_and_other_text_gives_none.
- X3. live_decision_reports_ram_budget_and_psi_pacing.

## Scope and reuse

- R1. Reuse the bounded host command adapter and the existing free-RAM writer budget.

## Open points for design approval

None.
