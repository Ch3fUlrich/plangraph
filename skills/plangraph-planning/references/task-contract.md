# Task contract (brief template)

The phase agent hands each implementer exactly this; nothing else is assumed.

```
TASK <id> | phase <phase-id> | layer 3 | role implementer
Objective:     <one sentence, one outcome>
Allowed files: <at most 3 source files + their tests>   (all other files are read-only)
Forbidden:     <API changes, new dependencies, unrelated refactors, repo-wide git operations>
Interfaces:    <signatures/contracts the task must honour; copied, not discovered>
Acceptance:    <one command; pass = exit 0 and "N passed">
Toolchain:     <lint, format, type-check, test commands, provided by the spawner>
Rules:         load `coding-principles`; failing test first; document the why
Worktree:      <branch/worktree name>; commit early; no repo-wide git operations
Return:        done report (schema), or error record on tool failure
```

Reviewer brief: same header, plus `Diff:` (commit range), `Lens:` (correctness | risk |
simplicity), and `Return: review findings` with severity and file:line. Reviewers are read-only
and never fix.

Fix-task brief: the original brief, plus `Fixes: <task id>`, the failing reviewer findings or the
error record, and "failure summary only; you have no memory of the previous attempt".

## Orchestrator briefs (L1 / L2)

Every L1 and L2 brief carries this clause verbatim (D-1272, operator 2026-10-13):

```
Small fixes: L1 writes no code. An L2 may write a fix itself only if it is <= ~10 changed lines
and touches no auth, access or secrets code; larger or such changes go to an L3 writer. The fix
needs 2 seats + the judge like any change, and the lane record and commit trailer say
`Writer: L2`.
```

Splitting heuristics: split on module, interface, test or migration boundary; merge tasks that
touch the same file; fold trivial edits into a neighbour with the same scope.

## Card mapping

Each brief becomes the card emitted in the same batch: `Objective:` -> `outcome`, `Allowed files:`
-> `write_set` (the rest of the repo goes to `read_set`, plus `forbid_paths`), `Acceptance:` ->
`check_cmd` (one command, exit 0), `Interfaces:` -> `exports`, `Forbidden:` -> `forbid_paths`,
`Toolchain:` -> `smoke_checks`. The planner adds `heldout_check` (hidden from the implementer),
`window_class`, explicit `risk_tags` (`[]` = reviewed, none) and an honest `estimate_tokens`.
