# Windows spawn/spawner baseline

Base: 3fa45c42; regenerated after the lane cleanup fix. Windows, `py -3`.

Exact command (bash; no manual --basetemp, no junit flag):
~~~sh
files=$(ls tests | grep -i spawn | grep -E .py | sed s#^#tests/#); PYTHONUTF8=1 PYTHONPATH=src py -3 -m pytest $files -q -p no:cacheprovider --tb=line -rfE
~~~
Output redirected to a local temporary log, not committed.
Totals: 68 failed, 559 passed, 284 skipped, 2 warnings, 4 errors in 415.77s (0:06:55).
no hang in 3 runs, no bound added (439 s, 446 s, 398 s).
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
- 40 cases in this selection: fake_ownership depends on Linux /proc birth identity, preserving
  real descendants while assigning fake identities to invisible stub PIDs.
  A Windows token-only stub would not preserve that distinction; retained.
  47 additional cases repo-wide (collection verified).
  Excluded suites: tests/test_spawn_cli.py (20),
  tests/test_spawn_win_stdin.py (6),
  tests/test_spawner_hardening_delivery.py (14).
  This excludes CLI cancellation/kill and Windows stdin/argv parity behaviour.
The review quoted 19 CLI and 7 stdin cases; collection verifies 20 and 6.
All three groups: owner: none yet; no dedicated owner item exists in this lane,
and implementation/fixture portability changes are deferred outside its scope.
Other existing test skips account for the remaining 65 skipped cases.

This run: finished; 68 failed, 559 passed, 284 skipped, 2 warnings, 4 errors in 415.77s (0:06:55).

<!-- generated Windows baseline -->
Recorded-at: 3fa45c42
Totals: 68 failed, 559 passed, 284 skipped, 2 warnings, 4 errors in 415.77s (0:06:55)
70 unique failure/error names; oversized parameter IDs are elided.
- tests/test_spawn_admission_render.py::test_runner_records_admitted_by_render | owner: none yet
- tests/test_spawn_cancel_d3505.py::test_d3505_cross_process_cancel_stops_the_leg_loop | owner: plangraph:SPAWNER-WIN-CANCEL-CHILD
- tests/test_spawn_win_render.py::test_parent_env_gateway_key_passthrough_and_no_leak | owner: none yet
- tests/test_spawn_win_render.py::test_render_unresolved_base_url_fails_closed | owner: none yet
- tests/test_spawn_win_render.py::test_unresolved_refusal_falls_through_to_next_leg | owner: none yet
- tests/test_spawner_cancel_cross_process.py::test_cancel_from_a_second_service_kills_the_child | owner: plangraph:SPAWNER-WIN-CANCEL-CHILD
- tests/test_spawner_context_guard.py::test_looped_run_cwd_has_specific_resolution_error[False] | owner: none yet
- tests/test_spawner_context_guard.py::test_looped_run_cwd_has_specific_resolution_error[True] | owner: none yet
- tests/test_spawner_context_guard.py::test_symlink_loops_fail_closed_not_with_a_runtime_error | owner: none yet
- tests/test_spawner_context_pack.py::test_line_count_streams_and_matches_splitlines[parameter elided: oversized ID] | owner: none yet
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
- tests/test_spawner_launch_spec.py::test_json[parameter elided: oversized ID] | owner: none yet
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

## Delta at #530 merge (L1 measured 2026-11-06)
Same command plus --continue-on-collection-errors, because three modules import fcntl and stop collection without it.
base b5d533b9: 70 failed, 680 passed, 284 skipped, 2 warnings, 7 errors in 546 s.
head 568d0d53: 70 failed, 726 passed, 284 skipped, 2 warnings, 7 errors in 517 s.
Failed and error names at base and head are identical: 75 names, 0 new, 0 gone. 46 more passed at head are the lane's new tests.
Not listed above, failing on both base and head:
- tests/test_evidence_spawner_record.py
- tests/test_evidence_spawner_store_path.py
- tests/test_mirror_spawner_records.py
- tests/test_spawner_context_pack.py::test_line_count_streams_and_matches_splitlines[x\nx\nx\nx\nx\nx\nx\nx\nx\nx\nx\nx\nx\nx
- tests/test_spawner_launch_spec.py::test_explicit_document_review[False
- tests/test_spawner_launch_spec.py::test_explicit_document_review[True
- tests/test_spawner_launch_spec.py::test_json[
- tests/test_spawner_launch_spec.py::test_tools[False-implement
- tests/test_spawner_launch_spec.py::test_tools[False-review
- tests/test_spawner_launch_spec.py::test_tools[True-implement
- tests/test_spawner_launch_spec.py::test_tools[True-review
Listed above but not failing at head:
- tests/test_spawner_cancel_cross_process.py::test_cancel_from_a_second_service_kills_the_child
- tests/test_spawner_context_pack.py::test_line_count_streams_and_matches_splitlines[parameter
- tests/test_spawner_host_bench.py::test_review_route_uses_benchmark_p
- tests/test_spawner_launch_spec.py::test_json[parameter
- tests/test_spawner_launch_spec.py::test_tools[implement
- tests/test_spawner_launch_spec.py::test_tools[review
Parametrized names are cut at the first space by the log filter.

## tests/test_changelog_fragments.py (L1 measured 2026-11-06, D-4346)
Same flags, this one file. Failure names are identical on base e6c48516, #546 head a07954d3 and merge af466362: 9 failed, 17 passed, 0 new, 0 gone. Not caused by #546.
- test_fold_preserves_and_reports_fragments_without_entries, 6 cases | owner: none yet
  The report prints changelog.d\PG-EMPTY.md; the test expects a forward slash.
- test_fold_uncommitted_fragments_sort_newest_by_mtime_then_name[edited, staged, untracked] | owner: none yet
  Order came back [24, 34, 14, 44], expected [14, 24, 34, 44]; cause not investigated.
