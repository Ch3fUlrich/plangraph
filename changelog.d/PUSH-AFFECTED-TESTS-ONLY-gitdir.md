The pre-push affected gate and direct selector strip repository-binding Git
variables before launching tests. Pytest also clears inherited bindings at
startup and before session fixtures, preventing temporary fixture Git commands
from mutating the caller repository. Discovery errors refuse test launches.

The mocked hook-gate unit test now supplies a disposable Git binding and explicitly asserts it is absent from the selector environment; a temporary pass-through mutation is detected.

All four test-entry boundaries delegate to the selector's canonical Git-environment scrub. The discovery contract now covers preflight as well, including case-insensitive future config variables and GIT_PREFIX. Disposable copied-checkout tests include that shared dependency.

The real conftest regression also runs with two xdist workers, each writing its own temporary checkout while the synthetic caller's refs, HEAD and config remain byte-identical. A fixture-only disabled-scrub mutation is detected by both workers.

Python session registry checks watch only test-owned temporary roots; config bytes and owned registration add/remove/move checks remain strict. Concurrent unrelated registrations and disappearance during scans no longer cause false teardown failures.
