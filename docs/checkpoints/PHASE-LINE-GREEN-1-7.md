# PHASE-LINE-GREEN-1 checkpoint 7

Checkpoint 7 includes review evidence records; run its affected Python checks in an isolated clone.
Python sessions compare only registrations pointing under their temporary test root, so other sessions may move worktrees concurrently; shared and per-worktree config bytes remain guarded. Worktree tests must create their repositories inside temporary roots; the guard reads metadata directly and never runs Git on the real checkout.
