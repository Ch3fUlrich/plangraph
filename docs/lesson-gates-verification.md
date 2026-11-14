# Package A2 verification boundary

The requested base is `95a9ac3423dec2bb7c6a6ca7f8d173ecbba868b5`.
A detached temporary checkout of that exact base reproduced the following
25 failures, without this package's changes. They were recorded without
xfail/skip; one affected mutation fixture is repaired as noted below.

- `tests/test_benchmark_data_contract.py::test_catalog_benchmark_contract`
- `tests/test_benchmark_data_contract.py::test_pending_allows_real_partial_resolution`
- `tests/test_providers_file.py::test_omniroute_leg_models_do_not_drift`
- `tests/test_run_exit_live.py::test_smoke_requires_live_readback[True]`
- `tests/test_run_prompt_record_seams.py::test_guard_checks_exact_allowlisted_call[stale_entry]`
- `tests/test_spawner_overflow.py::test_remote_prompt_bytes[300KiB]`
- `tests/test_spawner_overflow.py::test_remote_prompt_bytes[blank-line]`
- `tests/test_spawner_overflow.py::test_remote_prompt_bytes[newline]`
- `tests/test_spawner_overflow.py::test_remote_prompt_bytes[shell-literal]`
- `tests/test_spawner_overflow_caps.py::test_cli_shared_ram_refusal`
- `tests/test_spawner_seat_cwd.py::test_legacy_overflow_classifies_explicit_cwd[card]`
- `tests/test_spawner_seat_cwd.py::test_legacy_overflow_classifies_explicit_cwd[flag]`
- `tests/test_spawner_seat_cwd_round2.py::test_explicit_checkout_never_uses_healthy_remote[False-main]`
- `tests/test_spawner_seat_cwd_round2.py::test_explicit_checkout_never_uses_healthy_remote[True-main]`
- `tests/test_spawner_seat_cwd_round2.py::test_overflow_checks_selected_and_diff_repositories[False-False-card-main]`
- `tests/test_spawner_seat_cwd_round2.py::test_overflow_checks_selected_and_diff_repositories[False-False-flag-main]`
- `tests/test_spawner_seat_cwd_round2.py::test_overflow_checks_selected_and_diff_repositories[False-True-card-main]`
- `tests/test_spawner_seat_cwd_round2.py::test_overflow_checks_selected_and_diff_repositories[False-True-flag-main]`
- `tests/test_spawner_seat_cwd_round2.py::test_overflow_checks_selected_and_diff_repositories[True-False-card-main]`
- `tests/test_spawner_seat_cwd_round2.py::test_overflow_checks_selected_and_diff_repositories[True-False-flag-main]`
- `tests/test_spawner_seat_cwd_round2.py::test_overflow_checks_selected_and_diff_repositories[True-True-card-main]`
- `tests/test_spawner_seat_cwd_round2.py::test_overflow_checks_selected_and_diff_repositories[True-True-flag-main]`
- `tests/test_spawner_seat_fallthrough.py::test_recorded_capture_matches_registered_provider_identity`
- `tests/test_ui_hosts.py::test_persisted_registry_snapshot_hosts_page`
- `tests/test_w20_round8.py::test_corrupt_runs_skipped_and_recorded`

The initial affected-code run selected 526 Python test files from reverse imports
and explicit file references. CI/workflow and shared-fixture edits conservatively
select the entire suite; this package follows the explicit affected-only order
and checks the executable dependency set, plus its specific policy tests.

The package's introduced failures were reproduced and corrected. Existing
fallback-mechanics tests now explicitly authorize writer-family changes;
prompt-only stream tests supply a patch; the hermetic host has a synthetic client
credential. Pure planner enumeration remains independent of host configuration;
the spawner's service boundary enforces gateway and writer-family admission.

Linux tests use temporary roots and fake harnesses. Windows is cross-compiled;
no Windows host or live gateway was exercised. Remote CI, independent model
reviews, host timer activation and external proxy retirement remain outstanding.
See [the retirement boundary](local-gateway-retirement.md) for external work.

The outdated `stale_entry` prompt-record mutation fixture listed above is repaired
by targeting the current call site and asserting the mutation target exists.
That leaves 24 of the reproduced base failures outstanding.
