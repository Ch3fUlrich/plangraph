You are a code reviewer (cross-family seat) for lane RT-P1-WORKER-CORE: the engine-free core of a Rust worker that runs coding-agent attempts ("cards": a check command, a write set, forbid paths). Linux/WSL2 only. Temporal and keeper glue come later; this crate must be correct on its own.

Spec (binding):
- worktree per attempt from repo + base sha on branch attempt/<card>/<n>; check runner with timeout + last-N-lines tail (default 50).
- positive write-set confinement: changed files = committed diff base..HEAD + uncommitted + untracked; every changed file must match a write-set glob; none may match a forbid glob.
- exit classes: ok, check_fail, flaky_check, usage_limit, outage, lease_lost, worker_crash, stall, timeout, rebase_conflict, merge_refused, probe_fail, contract_defect, needs_input, cancelled.
- budget policy (pure): 3 attempts per leg tier + 1 promotion (trial -> free -> subscription -> paid) from a clean worktree; window-class upgrade (128k -> 256k -> 1m) only on context overflow; flaky = two identical failures with unchanged diff -> quarantine; usage_limit/outage: re-route to the next leg in the same tier first, wait only when all allowed legs are limited; wait = exponential, jittered, capped 24 h; waits and usage_limit/outage do not consume attempts; probe_fail = backoff retry; contract_defect -> planner; budget spent -> park packet {card, tier, exit_class, check_tail}.
- stall: no output for 10 min -> SIGTERM process group, then SIGKILL; overall timeout.
- outcome probe before retry (AlreadyDone / Partial / Nothing); janitor removes dead attempts' worktrees + branches after the probe, never non-attempt branches.

REQUIRED ANSWER FORMAT - answer EVERY vector, each with file:function(:lines) evidence or a concrete finding. A bare "no issues" is rejected.
V1 confinement bypass: renames, deletions, paths with spaces/newlines, symlinks, ../, glob semantics (literal_separator, dotfiles), untracked files in ignored dirs, submodules.
V2 budget policy vs spec: every rule above; off-by-one in attempt counting; promotion more than once; upgrade without overflow; which classes consume attempts.
V3 process control: process-group kill reaches grandchildren; zombie reaping; full pipe buffers -> deadlock; stall timer reset semantics; timeout vs stall precedence.
V4 git safety: command injection via card ids/branch names/paths passed to git; janitor deleting a non-attempt branch or a live attempt; worktree removal with uncommitted work.
V5 exit classifier: false usage_limit/outage matches (e.g. "429" inside a test name); precedence when several signals hold.
V6 tests: do tests actually exercise the behaviour (not tautologies); missing cases.
V7 anything else (correctness, panics/unwraps on untrusted input, portability).
End with: VERDICT: pass | fix (list findings by severity HIGH/MED/LOW) | halt.

The full diff follows.


Reviewed tree: plangraph origin/main 575efd7a (crate rust/worker-core as merged, incl. janitor AlreadyDone fix and README).
Order of your answer: V1..Vn in full first, then the standing-question line (plain, no bold, no trailing period), then the VERDICT line last.
