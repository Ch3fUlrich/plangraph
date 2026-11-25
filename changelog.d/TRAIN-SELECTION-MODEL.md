Add `pg-train-model`, an executable model of the landing-train actors (selection with the Overlap, Cap,
Target, ParentNotLanded, Conflict and Descendant dispositions and their verifier, binding, CI with a fix in
place for one red round, retry groups, the speculative next train, drop and revert at any stack depth,
legacy settlement, restart, main moving) with interleaving tests, an exhaustive search and a replay of
trains 9 to 16.
A retry train stale twice stops being rebuilt (D-6840 experiment): in tested fast-forward it lands on the current main as tested; in pull request merge mode an ordinary selection runs beside it. `Policy::stale_k` 0 is the plain per-group bound.
The required model settles a retry train only on the base and tree CI ran on; the D-6840 stale bound is an opt-in policy (`with_stale_bound`).
