The pre-push gate trims the shared cargo target before it builds: tools/preflight.py
takes the exclusive cargo lease (waits 5 s, else skips), prunes cli-tests/<checkout
hash> builds down to the newest PLANGRAPH_CLI_TESTS_KEEP (default 3) and none older
than 24 h, and removes the target when it is still over PLANGRAPH_CARGO_TARGET_CAP_GB
(default 12). Spawner admission shares the same reclaim code in host_ready.py.
