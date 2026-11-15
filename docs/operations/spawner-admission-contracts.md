# Spawner admission contracts

Freshness admission checks every worker checkout HEAD, including review/judge and pinned document runs; a fresh diff head cannot waive a stale worker checkout.

Only completed implement parents can start fixer continuations; review and judge findings never create implement children. Implement continuations retain both freshness checks.

Detached split-writer admission waits for every local child and preserves each refusal in the writers envelope; the CLI prints per-writer reasons and exits nonzero if any writer is refused.

An admission timeout cancels every worker started by that request, including already-admitted split siblings, and persists failure; runs that existed before the request keep running.

Admission timeout removes a created worktree when its worker never reached launch admission, using the same non-forced cleanup as a freshness refusal.

Refused worktree cleanup uses the attempt record’s selected checkout, including continuations whose repository differs from the runner default.

Review and judge freshness admission requires both the worker checkout HEAD and the reviewed diff head to contain origin’s default branch; the worker checkout witness remains the recorded checked base. Each distinct resolved repository fetches before comparison at both admission boundaries; the same repository reuses the worker fetch, and a separate diff repository failing its fetch is refused.

Review admission fetches the worker’s default branch once per admission boundary; the following diff-head ancestry check reuses tracking refs with `fetch=False`.

Freshness admission and refused-worktree cleanup use the shared checkout Git environment, stripping inherited object-store redirects and config injection as well as repository, index, and worktree overrides.
