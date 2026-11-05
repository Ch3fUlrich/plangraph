# Windows spawn/spawner baseline

Base: 40e1d7ee; regenerated after the lane cleanup fix. Windows, `py -3`.

Exact command (bash; no manual --basetemp, no junit flag):
~~~sh
files=$(ls tests | grep -i spawn | grep -E .py | sed s#^#tests/#); PYTHONUTF8=1 PYTHONPATH=src py -3 -m pytest $files -q -p no:cacheprovider --tb=line -rfE
~~~
Output redirected to a local temporary log, not committed.
Totals: 68 failed, 551 passed, 284 skipped, 2 warnings, 4 errors in 398.05s.
No hang reproduced in complete foreground runs (446.16s, 398.05s).
Suspect: resolver property calls in test_spawner_utility_props.py and
 test_spawner_utility_props_sweep.py; their SIGALRM cases remain excluded.
These runs establish no deadline for those excluded calls; no dependency added.
WinError 448 was not reproduced. Fresh-root cleanup leaves no new root:
5 pre-existing pg-win-pytest-* directories before and 5 after the final run.
Read-only git objects are retried; failed mkstemp assertions can leak open
CRT descriptors, so exit cleanup closes only descriptors matching owned files.
Linux and explicit --basetemp behavior are unchanged.

## Excluded coverage
- 138 cases / 132 functions: sessions_support.make() pins platform="linux",
  forcing the POSIX O_NOFOLLOW branch on Windows. Retained rather than changing
  fixture semantics across naming, audit, launch, relaunch, resolve, stop and
  config tests. The win32 config reader itself does not have that refusal.
- 41 cases: POSIX SIGALRM/setitimer resolver bounds unavailable on Windows.
- 40 cases: fake_ownership depends on Linux /proc birth identity, preserving
  real descendants while assigning fake identities to invisible stub PIDs.
  A Windows token-only stub would not preserve that distinction; retained.
  Excluded suites: tests/test_spawn_cli.py (20),
  tests/test_spawn_win_stdin.py (6),
  tests/test_spawner_hardening_delivery.py (14).
  This excludes CLI cancellation/kill and Windows stdin/argv parity behaviour.
The review quoted 19 CLI and 7 stdin cases; collection verifies 20 and 6.
All three groups: owner: none yet; no dedicated owner item exists in this lane,
and implementation/fixture portability changes are deferred outside its scope.
Other existing test skips account for the remaining 65 skipped cases.

## Remaining failures/errors
70 unique names cover 72 outcomes (two setup errors also error in teardown).
Names are verbatim except two explicitly elided oversized parameter IDs.
For every owner: none yet below, the shared reason is: no dedicated owner item
exists in this lane; these observed Windows failures require separate diagnosis,
not a speculative issue key. Existing cancellation ownership is preserved.

- tests/test_spawn_admission_render.py::test_runner_records_admitted_by_render | owner: none yet
- tests/test_spawn_cancel_d3505.py::test_d3505_cross_process_cancel_stops_the_leg_loop | owner: plangraph:SPAWNER-WIN-CANCEL-CHILD
- tests/test_spawn_win_render.py::test_parent_env_gateway_key_passthrough_and_no_leak | owner: none yet
- tests/test_spawn_win_render.py::test_render_unresolved_base_url_fails_closed | owner: none yet
- tests/test_spawn_win_render.py::test_unresolved_refusal_falls_through_to_next_leg | owner: none yet
- tests/test_spawner_cancel_cross_process.py::test_cancel_from_a_second_service_kills_the_child | owner: plangraph:SPAWNER-WIN-CANCEL-CHILD
- tests/test_spawner_context_guard.py::test_looped_run_cwd_has_specific_resolution_error[False] | owner: none yet
- tests/test_spawner_context_guard.py::test_looped_run_cwd_has_specific_resolution_error[True] | owner: none yet
- tests/test_spawner_context_guard.py::test_symlink_loops_fail_closed_not_with_a_runtime_error | owner: none yet
- tests/test_spawner_context_pack.py::test_line_count_streams_and_matches_splitlines[parameter elided: Windows environment limit] | owner: none yet
- tests/test_spawner_docs_contract.py::test_sensitivity_json_reproducible | owner: none yet
- tests/test_spawner_doctor.py::test_aggregate_exit_and_independence[good] | owner: none yet
- tests/test_spawner_doctor.py::test_harness[empty] | owner: none yet
- tests/test_spawner_doctor.py::test_harness[exit] | owner: none yet
- tests/test_spawner_doctor.py::test_harness[good] | owner: none yet
- tests/test_spawner_doctor.py::test_harness[hang] | owner: none yet
- tests/test_spawner_doctor.py::test_malformed_token_does_not_abort | owner: none yet
- tests/test_spawner_doctor.py::test_opt_in_probe[True] | owner: none yet
- tests/test_spawner_doctor.py::test_real_service_review_family | owner: none yet
- tests/test_spawner_doctor.py::test_text_output | owner: none yet
- tests/test_spawner_git_config_guard.py::test_fix_role_dangerous_mutation_fails_and_removes_key | owner: none yet
- tests/test_spawner_git_config_guard.py::test_mutated_config_on_cancelled_job | owner: none yet
- tests/test_spawner_git_config_guard.py::test_non_git_cwd_ok | owner: none yet
- tests/test_spawner_git_config_guard.py::test_process_benign_config_change_completes | owner: none yet
- tests/test_spawner_git_config_guard.py::test_process_unchanged_config_ok | owner: none yet
- tests/test_spawner_git_config_guard.py::test_sibling_branch_config_is_preserved[False] | owner: none yet
- tests/test_spawner_git_config_guard.py::test_sibling_branch_config_is_preserved[True] | owner: none yet
- tests/test_spawner_git_config_guard.py::test_writer_dangerous_mutation_fails_and_removes_key | owner: none yet
- tests/test_spawner_host_bench.py::test_review_route_uses_benchmark_p | owner: none yet
- tests/test_spawner_kill_own_tree.py::test_posix_killpg_refuses_unowned_group | owner: none yet
- tests/test_spawner_kill_own_tree.py::test_unproven_pid_timeout_ends_kill_incomplete_without_blocking | owner: none yet
- tests/test_spawner_launch_spec.py::test_cli[False-0] | owner: none yet
- tests/test_spawner_launch_spec.py::test_cwd_normalized | owner: none yet
- tests/test_spawner_launch_spec.py::test_dry | owner: none yet
- tests/test_spawner_launch_spec.py::test_events_warnings_and_current | owner: none yet
- tests/test_spawner_launch_spec.py::test_json[parameter elided: Windows environment limit] | owner: none yet
- tests/test_spawner_launch_spec.py::test_opencode | owner: none yet
- tests/test_spawner_launch_spec.py::test_response_write[BrokenPipeError] | owner: none yet
- tests/test_spawner_launch_spec.py::test_response_write[OSError] | owner: none yet
- tests/test_spawner_launch_spec.py::test_secret | owner: none yet
- tests/test_spawner_launch_spec.py::test_sweep | owner: none yet
- tests/test_spawner_launch_spec.py::test_sweep_unusable[False-[]] | owner: none yet
- tests/test_spawner_launch_spec.py::test_sweep_unusable[False-bad json] | owner: none yet
- tests/test_spawner_launch_spec.py::test_sweep_unusable[False-invalid_name] | owner: none yet
- tests/test_spawner_launch_spec.py::test_sweep_unusable[False-unreadable] | owner: none yet
- tests/test_spawner_launch_spec.py::test_sweep_unusable[False-{}] | owner: none yet
- tests/test_spawner_launch_spec.py::test_sweep_unusable[True-[]] | owner: none yet
- tests/test_spawner_launch_spec.py::test_sweep_unusable[True-bad json] | owner: none yet
- tests/test_spawner_launch_spec.py::test_sweep_unusable[True-invalid_name] | owner: none yet
- tests/test_spawner_launch_spec.py::test_sweep_unusable[True-unreadable] | owner: none yet
- tests/test_spawner_launch_spec.py::test_sweep_unusable[True-{}] | owner: none yet
- tests/test_spawner_launch_spec.py::test_tools[implement] | owner: none yet
- tests/test_spawner_launch_spec.py::test_tools[review] | owner: none yet
- tests/test_spawner_model_refusal.py::test_other_stops_unchanged[end_turn] | owner: none yet
- tests/test_spawner_model_refusal.py::test_other_stops_unchanged[max_tokens] | owner: none yet
- tests/test_spawner_model_refusal.py::test_refusal_record_and_output[assistant] | owner: none yet
- tests/test_spawner_model_refusal.py::test_refusal_record_and_output[partial] | owner: none yet
- tests/test_spawner_model_refusal.py::test_refusal_record_and_output[result] | owner: none yet
- tests/test_spawner_model_refusal.py::test_result_safety_stops_is_refusal | owner: none yet
- tests/test_spawner_model_refusal.py::test_route_is_terminal_only_for_claude[claude] | owner: none yet
- tests/test_spawner_round1.py::test_private_atomic_creation | owner: none yet
- tests/test_spawner_sessions_config.py::test_config_owner_mismatch_refused | owner: none yet
- tests/test_spawner_sessions_config.py::test_config_read_without_no_follow_refused | owner: none yet
- tests/test_spawner_sessions_inputs.py::test_default_config_path_used_when_env_unset | owner: none yet
- tests/test_spawner_sessions_mcp.py::test_mcp_tools_listed_and_dispatch | owner: none yet
- tests/test_spawner_sessions_names.py::test_stop_waits_for_a_launch_in_flight | owner: none yet
- tests/test_spawner_sessions_r6.py::test_mark_ready_blocked_by_name_lock | owner: none yet
- tests/test_spawner_silent_run.py::test_a_worktree_edit_after_the_late_line_keeps_the_run_alive | owner: none yet
- tests/test_spawner_silent_run.py::test_silent_child_ends_stalled_with_no_activity | owner: none yet
- tests/test_spawner_utf8.py::test_diff_cp1252 | owner: none yet
