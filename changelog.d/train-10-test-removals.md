# Train 10 test removals

## Main tests missing at the train head

Compared with `origin/main` at `c719af95`, one test name is absent.

- `writer_launch_refuses_a_workspace_write_readiness_record` in `rust/worker-core/tests/host_readiness.rs`. Retained part b62 replaced it with `writer_placement_requires_workspace_write_readiness`, which verifies that placement accepts workspace-write readiness and rejects danger-full-access readiness.

## Historical D-6608 record

The b33 turn supervision tests `host_reaps_a_writer_past_the_turn_deadline` and `host_reaps_a_writer_after_a_cancel_marker` were dropped from train 9 by revert `78a4e3e7d`. Both are already absent at `c719af95`.

## Tests of dropped parts (never on main), removed with their part

### b75 session and restart

- `rust/pg-spawnd/src/session/remote.rs`
  - `forward_timeout_keeps_the_transport_command_in_the_diagnostic`
- `rust/pg-spawnd/src/session/tests.rs`
  - `cap_policy_and_saved_override`
  - `future_attended_lease_is_deleted_and_window_is_bounded`
  - `legacy_fallback_prefers_the_next_enabled_host_and_can_warn_and_keep_local`
  - `numbered_role_and_repeated_options`
  - `subprocess_capture_times_out_without_waiting_for_exit`

The b75 custom fleet-launch parity checks for interrupted starts, restart, and watchdog recovery also left with the part.

### b78 late review, review health, and verdicts

- `rust/pg-spawnd/src/late_review.rs`
  - `delayed_completion_writes_the_verdict_bound_to_its_head`
  - `finisher_retires_pending_receipt_and_keeps_completion_recoverable`
- `rust/pg-spawnd/src/launch_tests.rs`
  - `deepseek_review_uses_the_catalogued_output_ceiling`
  - `native_codex_review_client_missing_has_a_distinct_exit`
  - `review_route_skips_mimo_pro_with_tools_denied_reason`
- `rust/pg-spawnd/src/review_health.rs`
  - `classifies_review_failures_from_record_evidence`
  - `demotes_only_after_four_of_ten_terminal_reviews_without_verdicts`
  - `reports_run_ids_using_record_answers_before_log_fallback`
- `rust/pg-spawnd/tests/placement_spawn.rs`
  - `completed_review_persists_classification`
- `rust/pg-spawnd/tests/placement_tools.rs`
  - `review_route_demotes_a_leg_with_four_missing_verdicts`
- `rust/plangraph-cli/tests/review_cmd.rs`
  - `late_review_file_marks_a_failed_or_killed_run`
  - `late_review_file_marks_a_missing_verdict`
  - `late_review_file_preserves_a_request_for_changes`
  - `live_leg_returns_pending_and_publishes_late_review_file`
  - `mixed_failed_and_live_seats_report_failure_and_keep_the_live_run`
- `rust/worker-core/src/review_text.rs`
  - `identified_part_continues_across_metadata`
  - `joins_consecutive_deltas_without_part_ids`
  - `joins_deltas_of_the_same_text_part`
  - `rendered_record_takes_precedence_over_split_stream`

### b79 attempt outcomes and routing effort

- `rust/pg-spawnd/src/runs_record_tests.rs`
  - `spawned_writer_and_seat_publish_effort_and_route`
- `rust/pg-spawnd/tests/attempt_outcome.rs`
  - `terminal_spawn_projects_final_leg_once`
- `tests/test_effort_level.py`
  - `test_effort_level_table`
  - `test_effort_profile_floors_and_planner_adjustments`
- `tests/test_keeper_cards.py`
  - `test_attempt_migration_from_existing_sqlite_store`
  - `test_keeper_card_roundtrips_computed_effort_decision`
- `tests/test_keeper_cli.py`
  - `test_attempts_cli_reads_writer_seat_and_adhoc_rows`
  - `test_attempts_preserve_fixer_and_cardless_item_identity`
  - `test_watchdog_delivers_spawn_through_keeper_http`
- `tests/test_planner_routing.py`
  - `test_hand_picked_plan_serializes_the_override_for_attempt_projection`

### b77 keeper lessons and class search

- `rust/pg-policy/tests/plan_item_trailers.rs`
  - `review_checklist_reports_fix_commits_without_a_class_search`
- `tests/hooks/test_fleet_report_status.py`
  - `test_fix_stop_holds_done_until_class_search_passes`
- `tests/test_keeper_api.py`
  - `test_incident_log_across_item_states`
  - `test_incident_migration_on_existing_sqlite_store`
- `tests/test_keeper_deps_ops.py`
  - `test_dependency_cap_counts_only_open_edges`
- `tests/test_keeper_schema_upgrade.py`
  - `test_pg_incident_index_upgrade_preserves_existing_keeper`
- `tests/test_task_context_guard_edges.py`
  - `test_open_pull_trailers_block_ready_and_all_claims_until_close`

Three deadline cases existed on the b77 part head and were excluded when it was folded into train 10. They were never on main.

- `rust/pg-policy/tests/test_sources.rs`
  - `a_declared_runner_must_have_the_named_override`
  - `a_second_nested_cargo_case_needs_its_own_build_override`
  - `an_override_for_another_case_does_not_cover_a_cargo_build`
