The pre-push rust stage bound now scales with the planned test binaries and the host load,
CARGO_BUILD_JOBS follows the free cores unless set (floor 4), and a timeout names the test
binary that was running, so a green train no longer fails its push on a loaded host.
