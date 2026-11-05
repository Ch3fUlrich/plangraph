# Windows spawn/spawner baseline

Code revision: 5e14c695 plus the Windows collection/temp changes in the accompanying commit.
Base: 40e1d7ee. Host: the Windows workstation; Python launcher: py -3.

Exact command (bash; no manual --basetemp):
~~~sh
PYTHONUTF8=1 PYTHONPATH=src py -3 -m pytest $(ls tests | grep -i spawn | grep -E '\.py$' | sed 's#^#tests/#') -q -p no:cacheprovider --tb=line -rfE
~~~

The recorded run additionally used --junitxml=<user path>
Totals: 68 failed, 550 passed, 285 skipped, 2 warnings, 4 errors in 439.07s.

No hang reproduced: initial run 424.43s; final run 439.07s, both foreground and complete.
Default TempPathFactory.getbasetemp() succeeded; WinError 448 was not reproduced.
The inherited Windows-only fresh-root workaround avoids numbered-root symlink cleanup;
Linux and explicitly supplied basetemp remain unchanged.

133 O_NOFOLLOW functions (139 cases) are excluded only after observed refusal.
Production gap: sessions_config.py:48-60 (_ConfigMixin._read_config_bytes) refuses
when a test selects the POSIX path on a Windows host; its win32 path reads normally.
Callers: _load_config (sessions_config.py:89), _launch_inner (sessions.py:154),
_mark_ready_inner (:315), _relaunch_inner (:331), _list_inner (:381),
and _names_mode (sessions_names.py:40). snapshot_sign.py:46 also refuses but is not
the source of these skips. SIGALRM helpers cannot safely interrupt arbitrary
resolver closures on Windows without a larger process-isolation rewrite; skip.
fake_ownership requires Linux /proc birth identity, so its users skip.

## Remaining failures/errors (verbatim pytest names)
Entries have an owner item where one exists, else owner: none yet; reasons are observed symptoms, not asserted root causes.
70 unique node IDs cover 72 outcomes: two setup errors also have teardown errors.
Two environment-size setup errors have huge generated parameter IDs, elided here as marked: PYTEST_CURRENT_TEST overflows the Windows environment limit.
- tests/test_spawn_admission_render.py::test_runner_records_admitted_by_render | owner: none yet; AssertionError: assert 'failed' == 'completed'
- tests/test_spawn_cancel_d3505.py::test_d3505_cross_process_cancel_stops_the_leg_loop | owner: plangraph:SPAWNER-WIN-CANCEL-CHILD; AssertionError: assert (1 == 1 and 'failed' == 'cancelled'
- tests/test_spawn_win_render.py::test_render_unresolved_base_url_fails_closed | owner: none yet; AssertionError: assert 'running' == 'failed'
- tests/test_spawn_win_render.py::test_parent_env_gateway_key_passthrough_and_no_leak | owner: none yet; AssertionError: assert '[autoos:redacted]' in ''
- tests/test_spawn_win_render.py::test_unresolved_refusal_falls_through_to_next_leg | owner: none yet; AssertionError: assert 'failed' == 'completed'
- tests/test_spawner_cancel_cross_process.py::test_cancel_from_a_second_service_kills_the_child | owner: plangraph:SPAWNER-WIN-CANCEL-CHILD; AssertionError: assert ('failed' == 'cancelled'
- tests/test_spawner_context_guard.py::test_symlink_loops_fail_closed_not_with_a_runtime_error | owner: none yet; AssertionError: Regex pattern did not match.
- tests/test_spawner_context_guard.py::test_looped_run_cwd_has_specific_resolution_error[False] | owner: none yet; Failed: DID NOT RAISE ContextError
- tests/test_spawner_context_guard.py::test_looped_run_cwd_has_specific_resolution_error[True] | owner: none yet; Failed: DID NOT RAISE ContextError
- tests/test_spawner_context_pack.py::test_line_count_streams_and_matches_splitlines[70000 x lines elided, then tail] | owner: none yet; Windows environment exceeds 32767 characters during setup
- tests/test_spawner_docs_contract.py::test_sensitivity_json_reproducible | owner: none yet; assert b'{\r\n  "cat...n  }\r\n}\r\n' == b'{\n  "catal...  }\n  }\n}\n'
- tests/test_spawner_doctor.py::test_harness[good] | owner: none yet; AssertionError: assert 'skip' == 'pass'
- tests/test_spawner_doctor.py::test_harness[empty] | owner: none yet; AssertionError: assert 'skip' == 'pass'
- tests/test_spawner_doctor.py::test_harness[exit] | owner: none yet; Failed: DID NOT RAISE Exception
- tests/test_spawner_doctor.py::test_harness[hang] | owner: none yet; Failed: DID NOT RAISE Exception
- tests/test_spawner_doctor.py::test_aggregate_exit_and_independence[good] | owner: none yet; AssertionError: assert 1 == 0
- tests/test_spawner_doctor.py::test_opt_in_probe[True] | owner: none yet; AssertionError: assert 1 == 0
- tests/test_spawner_doctor.py::test_text_output | owner: none yet; assert 1 == 0
- tests/test_spawner_doctor.py::test_malformed_token_does_not_abort | owner: none yet; AssertionError: assert 1 == 0
- tests/test_spawner_doctor.py::test_real_service_review_family | owner: none yet; AssertionError: {'checks': [{'name': 'mcp_spawner', 'status': 'pass', 'detail': '1 tools'}, {'name': 'mcp_keeper', 'status': 'pass', '...', 'detail': 'not on PATH'}, {'name': 'harness_missing', 'status': 'skip', 'detail'
- tests/test_spawner_git_config_guard.py::test_writer_dangerous_mutation_fails_and_removes_key | owner: none yet; AssertionError: assert 0 == 1
- tests/test_spawner_git_config_guard.py::test_process_benign_config_change_completes | owner: none yet; AssertionError: ([{'event': 'admitted_by_render', 'model': 'mimo/mimo-v2.6-flash'}, {'event': 'win_job_unavailable'}], '', 'harness supplied no authenticated served-model telemetry')
- tests/test_spawner_git_config_guard.py::test_sibling_branch_config_is_preserved[False] | owner: none yet; KeyError: 'job-0143ed88'
- tests/test_spawner_git_config_guard.py::test_sibling_branch_config_is_preserved[True] | owner: none yet; KeyError: 'job-73fe95eb'
- tests/test_spawner_git_config_guard.py::test_process_unchanged_config_ok | owner: none yet; AssertionError: ([{'event': 'admitted_by_render', 'model': 'mimo/mimo-v2.6-flash'}, {'event': 'win_job_unavailable'}], '', 'harness supplied no authenticated served-model telemetry')
- tests/test_spawner_git_config_guard.py::test_non_git_cwd_ok | owner: none yet; AssertionError: ([{'event': 'admitted_by_render', 'model': 'mimo/mimo-v2.6-flash'}, {'event': 'win_job_unavailable'}], '', 'harness supplied no authenticated served-model telemetry')
- tests/test_spawner_git_config_guard.py::test_fix_role_dangerous_mutation_fails_and_removes_key | owner: none yet; assert False
- tests/test_spawner_git_config_guard.py::test_mutated_config_on_cancelled_job | owner: none yet; KeyError: 'job-92339d9b'
- tests/test_spawner_host_bench.py::test_review_route_uses_benchmark_p | owner: none yet; AssertionError: assert ['qodercli-qw...glm-5.2', ...] == ['qodercli-qw...4-argon', ...]
- tests/test_spawner_kill_own_tree.py::test_posix_killpg_refuses_unowned_group | owner: none yet; AttributeError: <module 'os' (frozen)> has no attribute 'killpg'
- tests/test_spawner_kill_own_tree.py::test_unproven_pid_timeout_ends_kill_incomplete_without_blocking | owner: none yet; KeyError: 'job-b042dbf4'
- tests/test_spawner_launch_spec.py::test_json[...16777216 spaces elided...] | owner: none yet; Windows environment exceeds 32767 characters during setup
- tests/test_spawner_launch_spec.py::test_tools[implement] | owner: none yet; AssertionError: assert (1, 'launch-s... (PATHEXT)\n') == (0, '')
- tests/test_spawner_launch_spec.py::test_tools[review] | owner: none yet; AssertionError: assert (1, 'launch-s... (PATHEXT)\n') == (0, '')
- tests/test_spawner_launch_spec.py::test_opencode | owner: none yet; AssertionError: assert (1, 'launch-s... (PATHEXT)\n') == (0, '')
- tests/test_spawner_launch_spec.py::test_dry | owner: none yet; AssertionError: assert (1, 'launch-s... (PATHEXT)\n') == (0, '')
- tests/test_spawner_launch_spec.py::test_response_write[BrokenPipeError] | owner: none yet; AssertionError: assert 'launch-spec ...e (PATHEXT)\n' == 'launch-spec ...enPipeError\n'
- tests/test_spawner_launch_spec.py::test_response_write[OSError] | owner: none yet; AssertionError: assert 'launch-spec ...e (PATHEXT)\n' == 'launch-spec ...ed: OSError\n'
- tests/test_spawner_launch_spec.py::test_cwd_normalized | owner: none yet; AssertionError: assert (1, 'launch-s... (PATHEXT)\n') == (0, '')
- tests/test_spawner_launch_spec.py::test_sweep_unusable[False-invalid_name] | owner: none yet; assert 1 == 0
- tests/test_spawner_launch_spec.py::test_sweep_unusable[False-{}] | owner: none yet; assert 1 == 0
- tests/test_spawner_launch_spec.py::test_sweep_unusable[False-bad json] | owner: none yet; assert 1 == 0
- tests/test_spawner_launch_spec.py::test_sweep_unusable[False-[]] | owner: none yet; assert 1 == 0
- tests/test_spawner_launch_spec.py::test_sweep_unusable[False-unreadable] | owner: none yet; assert 1 == 0
- tests/test_spawner_launch_spec.py::test_sweep_unusable[True-invalid_name] | owner: none yet; assert 1 == 0
- tests/test_spawner_launch_spec.py::test_sweep_unusable[True-{}] | owner: none yet; assert 1 == 0
- tests/test_spawner_launch_spec.py::test_sweep_unusable[True-bad json] | owner: none yet; assert 1 == 0
- tests/test_spawner_launch_spec.py::test_sweep_unusable[True-[]] | owner: none yet; assert 1 == 0
- tests/test_spawner_launch_spec.py::test_sweep_unusable[True-unreadable] | owner: none yet; assert 1 == 0
- tests/test_spawner_launch_spec.py::test_sweep | owner: none yet; assert 1 == 0
- tests/test_spawner_launch_spec.py::test_secret | owner: none yet; assert (1 == 0)
- tests/test_spawner_launch_spec.py::test_cli[False-0] | owner: none yet; AssertionError: assert 1 == 0
- tests/test_spawner_launch_spec.py::test_events_warnings_and_current | owner: none yet; plangraph.spawner.launch_spec.WindowsEnvIncomplete: PATHEXT
- tests/test_spawner_model_refusal.py::test_refusal_record_and_output[result] | owner: none yet; AssertionError: assert ('failed', 'kill_incomplete') == ('failed', 'model_refusal')
- tests/test_spawner_model_refusal.py::test_refusal_record_and_output[assistant] | owner: none yet; AssertionError: assert ('failed', 'kill_incomplete') == ('failed', 'model_refusal')
- tests/test_spawner_model_refusal.py::test_refusal_record_and_output[partial] | owner: none yet; AssertionError: assert ('failed', 'kill_incomplete') == ('failed', 'model_refusal')
- tests/test_spawner_model_refusal.py::test_other_stops_unchanged[end_turn] | owner: none yet; AssertionError: assert ('failed' == 'completed'
- tests/test_spawner_model_refusal.py::test_other_stops_unchanged[max_tokens] | owner: none yet; AssertionError: assert ('failed' == 'completed'
- tests/test_spawner_model_refusal.py::test_result_safety_stops_is_refusal | owner: none yet; KeyError: 'stop_reason'
- tests/test_spawner_model_refusal.py::test_route_is_terminal_only_for_claude[claude] | owner: none yet; AssertionError: assert 'kill_incomplete' == 'model_refusal'
- tests/test_spawner_round1.py::test_private_atomic_creation | owner: none yet; AssertionError: assert (33206 & 511) == 384
- tests/test_spawner_sessions_config.py::test_config_read_without_no_follow_refused | owner: none yet; AttributeError: O_NOFOLLOW
- tests/test_spawner_sessions_config.py::test_config_owner_mismatch_refused | owner: none yet; AttributeError: <module 'os' (frozen)> has no attribute 'getuid'
- tests/test_spawner_sessions_inputs.py::test_default_config_path_used_when_env_unset | owner: none yet; plangraph.routing_errors.ParameterRefused: sessions config is not a readable regular non-symlink file: <user home>/.config/plangraph/sessions.json
- tests/test_spawner_sessions_mcp.py::test_mcp_tools_listed_and_dispatch | owner: none yet; assert False is True
- tests/test_spawner_sessions_names.py::test_stop_waits_for_a_launch_in_flight | owner: none yet; assert False
- tests/test_spawner_sessions_r6.py::test_mark_ready_blocked_by_name_lock | owner: none yet; KeyError: 'res'
- tests/test_spawner_silent_run.py::test_silent_child_ends_stalled_with_no_activity | owner: none yet; AssertionError: assert ('failed' == 'stalled'
- tests/test_spawner_silent_run.py::test_a_worktree_edit_after_the_late_line_keeps_the_run_alive | owner: none yet; AssertionError: assert ('failed' == 'timeout'
- tests/test_spawner_utf8.py::test_diff_cp1252 | owner: none yet; Failed: DID NOT RAISE UnicodeDecodeError
