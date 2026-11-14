# CI tool downloads

Temporal CLI 1.9.1 uses the shared `setup-temporal` action in all three jobs.
Cache misses try GitHub briefly, then the official Temporal CDN. Both sources
must pass the same pinned archive SHA-256 check before extraction and caching.

The pytest job caches gitleaks 8.30.1 separately by tool, version, OS and
architecture. A miss briefly tries the release archive and checksums file;
missing checksum lines or mismatches fail closed. Transport failures fall back
to `go install github.com/zricethezav/gitleaks/v8@v8.30.1` using the official
Go module proxy and public checksum database, with private-module exclusions
cleared. Source builds may report a development CLI version, so their identity
comes from the pinned, checksum-database-verified module instead of linker flags.
Only the verified binary is cached; cache actions are pinned by full SHA.

Offline workflow guards scan workflows and composite actions for all release
URLs, Temporal CDN URLs and Go installs. Archive listing is not verification.

## Self-hosted Linux / WSL2 runner provisioning

Before enabling `CI_SELF_HOSTED=1` and `CI_LINUX_RUNNER_LABELS`, run this
from the checkout as **each runner user** (no sudo):

```sh
bash deploy/bootstrap.sh --ci-tools-only
```

This mode only installs CI tools; it does not enroll a spawner host or install
watchdogs. It installs gitleaks **8.30.1** in `~/.local/bin/gitleaks`, which the
public PEM gate test also searches when that directory is absent from PATH.
Re-run the command to replace a missing or stale binary. `--dry-run` prints the
selected version, architecture, checksum and destination without downloading.
The bootstrap verifies the release archive before extracting or installing;
transport, checksum and CLI version failures stop provisioning.

Pinned release archive SHA-256 values (from the 8.30.1 release checksums):

| Archive | SHA-256 |
| --- | --- |
| `gitleaks_8.30.1_linux_x64.tar.gz` | `551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb` |
| `gitleaks_8.30.1_linux_arm64.tar.gz` | `e4a487ee7ccd7d3a7f7ec08657610aa3606637dab924210b3aee62570fb4b080` |

Source: <https://github.com/gitleaks/gitleaks/releases/download/v8.30.1/gitleaks_8.30.1_checksums.txt>.
The hosted workflow's verified download/Go fallback remains described above.
