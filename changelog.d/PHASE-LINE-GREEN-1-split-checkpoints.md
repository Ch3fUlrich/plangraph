Spawner daemon tests isolate Git fixture commands from inherited Git controls and host configuration.

Worker crate is included in this checkpoint.

Preflight distinguishes standalone workspace ownership, follows path dependents (including worker-core to worker), and preserves the Temporal spike replay-only check.
Worker cargo test subprocesses also receive CI-compatible test-only jail defaults without changing caller settings or other check environments.

Refresh validation respects the shared Cargo target in placement fixtures and nested admission checks; live scope tests accept an empty isolated HOME.

Worker preflight selects offline library and binary tests; the complete Temporal integration suite remains in CI with its isolated server wrapper.
