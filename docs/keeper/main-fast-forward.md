# Keeper loaded-code identity and main fast-forward holds

At daemon startup, before requests are served, the keeper atomically writes a
private file beside its configured DB anchor, named `<db filename>.loaded.json`.
It contains `loaded_sha`, `pid`, wall-clock `start_time`, and the process-start
`/proc` token when available. The SHA describes the spawner-independent keeper
code tree at startup; it is not reread after checkout moves. Missing Git identity
is recorded as unknown, which makes a running keeper hold updates. Failure to
publish the state prevents serving rather than leaving an unguarded live process.
Clean shutdown removes the file before releasing the keeper lock; stale crash
state is rejected using PID/start-token identity by the updater.

Loaded state includes `loaded_branch` (null for detached HEAD) and `dirty`.
A clean detached checkout has an exact SHA and can be compared normally. Tracked
local changes make `loaded_sha` unknown, so updates hold until a clean restart;
untracked files are not a code identity. A non-Git installation records unknown
SHA/branch/dirt and also holds indefinitely until restarted from identifiable Git
code (or the keeper is stopped); no arbitrary HEAD is asserted for those installs.
