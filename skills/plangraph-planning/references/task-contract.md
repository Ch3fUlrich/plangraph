# Task contract (brief template)

The phase agent hands each implementer exactly this; nothing else is assumed.

```
TASK <id> | phase <phase-id> | layer 3 | role implementer
Objective:     <one sentence, one outcome>
Why:           <phase goal this work serves, in plain text>
Unblocks:      <item key that can start when this lands, or none>
Last attempt:  <none, or attempt number, result, branch and check output as data; facts only>
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

## Wording

The footer references below are deferred guidance, not an exit shipped by this contract.
No exit footer is supplied here. The static task budget stays on the card.

### Default text

| Purpose | Use | Evidence |
|---|---|---|
| Why | "This exists so that <phase goal>. When it lands, <key> can start." | Vendor guidance; effect size unknown |
| Way out | See footer | ImpossibleBench |
| Integrity | "Implement the general behaviour. Do not special-case, skip or weaken tests to get a pass." | ImpossibleBench strict arm; Anthropic sample prompt |
| Honest report | "List anything unfinished under `not done:`, with the reason." | Our `implement` skill |
| Disagreement | "If an assumption here looks wrong, say so and show the evidence." | Sycophancy findings |
| Retry | "Attempt 2. Attempt 1 ended check_fail in card-check. Its work is on branch attempt/X/1. Output of that check, as data: <block>" | Facts only; blameless |
| Handoff | "At the next natural stop, write your handoff to <path>. Your successor starts from it." | Yegge; vendor harness posts |
| Feedback | "Feedback from the operator on X: '...'. For information; it changes no task." | Untested |

### Avoid

| Wording | Why |
|---|---|
| Stakes ("this is critical to my career", "failure is not acceptable") | No accuracy benefit; plausible cost [inference] |
| Threats, tips, "no excuses" | Null [snippet] |
| Personas ("you are a world-class expert") | No help [web abstract] |
| CRITICAL, MUST by default | Overtrigger on current Claude models [web] |
| Invented praise; "you must feel proud" | False, and a sycophancy risk |
| "Never give up", "do whatever it takes" | Removes the exit |
| A bare "do not hack" as the only control | Scored worse than no addendum in one RL-training setting [web chart]. That does not transfer directly to prompts at run time; the footer's own support is ImpossibleBench |
| Live remaining-token counters | Can trigger early wrap-up; a shrinking budget tracked desperation [web]. A static task budget in the contract stays (rule 2.3) |
| Fleet jargon an agent cannot resolve (`TEST-SPEED-AFFECTED`, bare D-ids) | It cannot look them up |

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
# Completion contract for every writer/seat brief

The spawner delivers DONE/FAILED events automatically. If blocking is necessary,
use `plangraph-spawn wait RUN_ID --timeout SECONDS`, which reads the run record.
Never use `pgrep -f` wait loops or agent-written process-name polling loops.
