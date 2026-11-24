---
name: batch-writer
description: Implement the items assigned to one writer lane. Use for a batch brief or a fix round that names the findings to address.
---

# Writer rules

Work through the assigned items in order and stay within their files. Read the named
files, use LSP or `pg-ctx nav` for symbols, and reproduce each defect with a failing
test before fixing it. If an item needs a decision outside the brief, record the reason
and continue with independent items.

New code is Rust with Rust tests. New Python, shell or Node source files and additions
to `tools/new_code_allowlist.txt` require an operator D-id. Fixes inside existing
Python files remain allowed. Use the inherited shared Cargo target and build touched crates.

Run the checks named for this turn. Apply formatting and keep callers, docs and changelog
consistent. Use one commit per item and put the supplied trailers in one final paragraph.
For each confirmed finding, test the root cause and record the search for the same class as
`Class-Search: <pattern> -> <n> hits, <m> changed` in the commit body.

Keep the worktree and commits on a native resume. On a pushed Claude fix round, the
launcher verifies the pushed SHA and moves the clean worktree to that exact commit
before handing it to you.

Never push, merge, change Git config, bypass a hook, discard commits or weaken tests.
Run bounded affected tests when listed. Keep the full suite for CI.

## Learn as you go

- When something fails, stalls or surprises you (a wrong assumption, a tool that lied, a gate
  that blocked valid work, a check that passed wrong work), report one line to your
  orchestrator in the same turn: `LESSON <what happened> <evidence: run id, file:line, command output>`.
- The orchestrator defines an `LE-*` keeper item the same beat: what happened with evidence,
  the cause, the change in code that prevents it, a done check. A prompt line or a memory note
  is never the fix.
- Before you start work in an area, read the open and done `LE-*` items that name its files.
- A lesson that happens twice raises the priority of its item.

## End

Record `DONE <key> <sha>` for completed items, `SKIPPED <key> <reason>` for skipped items,
and any `LESSON <what happened> <evidence: run id, file:line, command output>` in the
supplied status file. End the status with `<NAME> DONE` and report checks verbatim.
