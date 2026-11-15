The pre-push affected gate and direct selector strip repository-binding Git
variables before launching tests. Pytest also clears inherited bindings at
startup and before session fixtures, preventing temporary fixture Git commands
from mutating the caller repository. Discovery errors refuse test launches.
