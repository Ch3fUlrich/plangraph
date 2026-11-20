The pre-push affected-test step sizes its pytest workers from the host (half the cores, one per 2 GiB of available memory from the spawner's memory probe, 2 to 16, PREFLIGHT_TEST_WORKERS overrides) instead of a fixed 2.
The spawner memory probe answers ValueError (not IndexError) for a MemAvailable line without a value.
