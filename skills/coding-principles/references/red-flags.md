# Red flags for Principles 1-9

Read when you catch yourself reasoning toward a shortcut. Each row is an excuse and its answer.

Contents: P1 DRY, P2 TDD, P3 single responsibility, P4 documentation, P5 backtracking,
P6 navigation, P7 scratch files, P8 coupled state (with the failures it prevents), P9 production path.

## Principle 1 — DRY / Single Source of Truth

| Red flag | Reality |
|---|---|
| "I'll copy this block and tweak it" | Two copies drift. Extract or parameterize. |
| "It's only duplicated twice" | The third copy is coming. Extract now. |
| "I'll keep the README and the code in sync by hand" | You won't. Generate or reference. |

## Principle 2 — Test-Driven Development

| Red flag | Reality |
|---|---|
| "I'll add tests after" | After never comes, and the test can't fail-first. |
| "This is too simple to test" | Simple code breaks too; the test is cheap. |
| "The test passed on the first try" | Did you see it fail? If not, it may test nothing. |

## Principle 3 — Single Responsibility

| Red flag | Reality |
|---|---|
| "This helper does X and also Y" | Two responsibilities. Two units. |
| "One more flag on this function" | Flags that switch behavior are a split signal. |

## Principle 4 — Documentation

| Red flag | Reality |
|---|---|
| `// increment i by 1` | Restates code. Delete or explain *why*. |
| "Docs update is a separate PR" | Docs drift from code. Same change or it's wrong. |

## Principle 5 — Backtracking

| Red flag | Reality |
|---|---|
| "One big commit at the end" | Un-revertible, un-reviewable. Commit per task. |
| "Everyone knows why we did this" | They won't in six months. Write the ADR. |

## Principle 6 — Navigation

| Red flag | Reality |
|---|---|
| "Let me read the whole file to find this function" | Use LSP or `find_symbol`. Cheaper and exact. |
| "I'll re-derive how this project works" | Read the file or item that states the rule. A graph node is a projection of it, never its source. |
| "The diff looks right, the page must render" | You changed the UI and never opened it. Drive it with Playwright. |

## Principle 7 — Scratch files

| Red flag | Reality |
|---|---|
| "I'll just drop `scratch_test.py` in the repo root" | Pollutes root/source tree. Use connected scratch dir. |
| "It's temporary, I'll delete it later" | It gets left behind. Create it in `scratch/` from the start. |

## Principle 8 — Coupled state

The failures Principle 8 exists to prevent all look identical afterwards and are
invisible beforehand:

- files were merged and renumbered, but the job database still named the old
  paths — and for one case that old path *existed* and held the pre-merge file,
  so staging silently re-fetched it over the new one;
- a record's ids were corrected in one store while two upstream sources kept the
  old ones, so the next sync restored them;
- work was re-queued to re-run while its previous results stayed in place, and
  the integrity guard refused every item;
- a hand-edited input left its *derived* fields frozen, and gigabytes were
  written under a placeholder name.

## Principle 9 — Production path

No table. The one question: which branch does production take, and did your check take it?
