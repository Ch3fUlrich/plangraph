# Snapshot serve: pinned checkout

Pinned checkout (LE-SNAPSHOT-SERVE-PATH, D-3445): the forced command runs from the pinned
checkout `~/.local/share/plangraph/snapshot-serve` (`PYTHONPATH=<checkout>/src`), refreshed ONLY
by the live-sync step — never from a `.claude/worktrees/` checkout (session cleanup removes
those and pulls then break silently). AGENTS NEVER EDIT `authorized_keys`; changing the forced
command's path is operator step OS-SNAPSHOT-PULL-PATH only. Helpers:
`python3 -m plangraph.keeper.snapshot_serve_path print --checkout P --config C` prints the exact
forced command to install; `python3 -m plangraph.keeper.snapshot_serve_path check
--authorized-keys FILE` flags snapshot-serve lines whose PYTHONPATH dir is missing or under
`.claude/worktrees/` (exit 1 on problems, 0 clean).
