# Keeper loaded-code identity and main fast-forward holds

At daemon startup, before requests are served, the keeper atomically writes a
private file beside its configured DB anchor, named `<db filename>.loaded.json`.
It contains `loaded_sha`, `pid`, wall-clock `start_time`, and the process-start
`/proc` token when available. The SHA describes the spawner-independent keeper
code tree sampled before package imports and verified after publication.
A changed identity during startup records unknown; it is not refreshed while serving. Missing Git identity
is recorded as unknown, which makes a running keeper hold updates. Failure to
publish the state refuses serving with a named error and nonzero exit, without a traceback.
Clean shutdown removes the file before releasing the keeper lock; stale crash
state is rejected using PID/start-token identity by the updater.

Loaded state includes `loaded_branch` (null for detached HEAD) and `dirty`.
A clean detached checkout has an exact SHA and can be compared normally. Tracked
local changes make `loaded_sha` unknown, so updates hold until a clean restart;
untracked files are not a code identity. A non-Git installation records unknown
SHA/branch/dirt and also holds indefinitely until restarted from identifiable Git
code (or the keeper is stopped); no arbitrary HEAD is asserted for those installs.
