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
