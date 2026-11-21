The pre-push gate trims the shared cargo target before it builds: tools/preflight.py
takes the exclusive cargo lease (waits 5 s, else skips), prunes cli-tests/<checkout
hash> builds down to the newest PLANGRAPH_CLI_TESTS_KEEP (default 3) and none older
than 24 h, and prunes policy-bin/<hash> builds the same way, removes the target when it is still over PLANGRAPH_CARGO_TARGET_CAP_GB
(default 12). Preflight refuses a target that holds or sits in the checkout or home, is a Git checkout, or has no Cargo output of its own (a CACHEDIR.TAG with Cargo's signature or a real .rustc_info.json, at its top or in a cli-tests/policy-bin build); marker names alone and symlinked build roots are never trusted. Spawner admission shares the same reclaim code in host_ready.py.
