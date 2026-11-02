# Rule Disposition

One row per rule id in the skills rewrite set. `delete` rows are dropped from the
rewritten skill, `code` rows are enforced by a named test, `card` rows move to a
role card.

| id | disposition | enforcement | why |
| --- | --- | --- | --- |
| R-router-01 | delete | - | a dialog blocks a background session |
| R-router-02 | delete | - | first verdict is usually wrong |
| R-router-03 | delete | - | a router doing work stops routing |
| R-router-04 | delete | - | sweep permission requests every beat |
| R-router-05 | delete | - | task L1s through L1-main only |
| R-router-06 | delete | - | aborted POST may still have landed |
| R-router-07 | delete | - | finish=None with no output is a dead run |
| R-router-08 | delete | - | check headroom before fan-out |
| R-router-09 | delete | - | serve restart cuts in-flight runs |
| R-router-10 | delete | - | mirror container edits to host source |
| R-router-11 | delete | - | operator questions are last resort |
| R-heartbeat-01 | delete | - | fleet-wide beat checks |
| R-heartbeat-02 | delete | - | remedies by finding |
| R-heartbeat-03 | delete | - | record the beat in state |
| R-heartbeat-04 | delete | - | clean-idle orchestrator gets a resume |
| R-heartbeat-05 | delete | - | ping the interactive router |
| R-coord-01 | code | none (gap) | lane→orch→main no-ff, one merger |
| R-coord-02 | card | role card: planner | cheap done must be judged |
| R-coord-03 | card | role card: planner | Claude orchestrates, never implements |
| R-coord-04 | code | none (gap) | headroom keeps tests and builds alive |
| R-coord-06 | delete | - | successor resumes from state alone |
| R-coord-07 | delete | - | an idle session is retired after 8 h |
| R-coord-08 | delete | - | stale orders ran workers post-stop |
| R-coord-09 | code | tests/test_spawner_import_guard.py::test_main_clean | hand-rolled spawns drift from gates |
| R-coord-10 | code | tests/test_spawn_cli.py::test_cancel_real_child | cancel must leave no surviving child |
| R-coord-11 | code | none (gap) | ff MCP checkout before restart |
| R-coord-12 | code | tests/test_keeper_commit_closes.py::test_commit_gated_close_fold_and_verify_integrity_agree | ready refuses an unrecorded sha |
| R-coord-13 | delete | - | parents must be watchable and addressable |
| R-coord-14 | delete | - | orchestrators that work stop orchestrating |
| R-coord-15 | code | none (gap) | operator-only gateway key writes |
| R-orch-01 | card | role card: orchestrator | fixed fields parse and stay short |
| R-orch-02 | card | role card: orchestrator | one file, exact spec, anchors |
| R-orch-04 | card | role card: orchestrator | isolated workers get inline or absolute paths |
| R-orch-06 | card | role card: orchestrator | relaunch, never resume a no-change child |
| R-orch-08 | code | none (gap) | record classifier refusals verbatim |
| R-orch-10 | card | role card: orchestrator | privileged changes get Sonnet final |
| R-orch-11 | code | none (gap) | grep every route-id consumer |
| R-orch-12 | code | none (gap) | trust_worktree before first session |
| R-orch-13 | card | role card: planner | cross-family review for big diffs |
| R-orch-14 | card | role card: planner | record writer, reviewer, verdict |
| R-orch-15 | card | role card: planner | one lesson line per bug |
| R-orch-16 | card | role card: planner | re-audit code that gates |
| R-orch-17 | code | tests/test_spawn_cli.py::test_bounded_wait_and_private_record | kill targets only from runner-private record |
| R-orch-18 | code | none (gap) | registry verify beyond pytest |
| R-orch-19 | code | tests/test_stall_watchdog.py::test_real_spawn_record_parent_and_item_come_from_completion | mocked tests miss live spawn breaks |
| R-orch-20 | code | tests/test_planner_readiness.py::test_write_sets_disjoint_reports_pairs | spread sessions across leg-disjoint routes |
| R-orch-21 | delete | - | tool call first in every turn |
| R-orch-22 | delete | - | end every wave with a visible block |
| R-orch-23 | delete | - | abandon a stuck child |
| R-orch-24 | delete | - | read memory.max before first spawn |
| R-orch-25 | delete | - | /tmp wipes on restart |
| R-orch-26 | delete | - | primary-mode orchestrator sessions |
| R-orch-27 | code | tests/test_secret_transfer.py::test_secret_path_never_in_argv_and_real_tools_not_used | secrets never printed to argv |
| R-orch-28 | delete | - | stand the old session down explicitly |
| R-orch-29 | code | none (gap) | pin 1M combos for orchestrator seats |
| R-orch-30 | code | none (gap) | fan out at most 10 seats |
| R-worker-01 | card | role card: worker | derive cells from registry render |
| R-worker-02 | card | role card: worker | portable failing-first tests |
| R-worker-03 | card | role card: worker | keep runs cheap |
| R-worker-04 | card | role card: worker | fakes mirror the real tool contract |
| R-worker-05 | code | none (gap) | gate on pipefail and the passed line |
| R-worker-06 | code | none (gap) | leaf roles never spawn |
| R-worker-07 | code | none (gap) | never shellcheck run-tests.sh locally |
| R-worker-08 | code | tests/test_run_watchdog.py::test_detached_grandchild_bounds_drain | verify on a detached copy, never cp |
| R-worker-09 | code | none (gap) | never activate_project from a worktree |
| R-worker-10 | card | role card: worker | redact on the real corpus |
| R-worker-11 | card | role card: worker | harness compaction vs peer wrapper |

## Gaps

Code rules with `none (gap)` enforcement. Each needs a named test written in a
later slice before its enforcement cell can name a `path::test_name`:

- R-coord-01: needs a named test for lane→orch→main no-ff merge ordering.
- R-coord-04: needs a named test for headroom limits and lane cap.
- R-coord-11: needs a named test for ff-before-MCP-restart ordering.
- R-coord-15: needs a named test refusing writes to gateway key/config files.
- R-orch-08: needs a named test for verbatim classifier refusal recording.
- R-orch-11: needs a named test that route-id consumers are all grepped.
- R-orch-12: needs a named test for trust_worktree before first session.
- R-orch-18: needs a named test for registry render checks beyond pytest.
- R-orch-29: needs a named test pinning 1M combos for orchestrator seats.
- R-orch-30: needs a named test for the 10-seat fan-out cap.
- R-worker-05: needs a named test for pipefail plus passed-line gating.
- R-worker-06: needs a named test that leaf roles never spawn.
- R-worker-07: needs a named test forbidding local shellcheck of run-tests.sh.
- R-worker-09: needs a named test forbidding activate_project from a worktree.
