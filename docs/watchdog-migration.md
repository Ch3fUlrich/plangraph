# Watchdog module migration

The watchdog CLI now lives in `plangraph.spawner.stall_watchdog`. Update external
routerctl integrations and scheduled commands to use that module with `status`,
`tick`, or `install`. The status file remains `watchdog/status.txt` under the state
directory. There is intentionally no legacy Python shim: importing the spawner
outside the planner routing boundary would violate the package import guard.

`test_old_watchdog_module_not_referenced` checks in-repository callers;
`test_fixture_red` in `tests/test_spawner_import_guard.py` rejects a misplaced
watchdog shim that imports the spawner.
