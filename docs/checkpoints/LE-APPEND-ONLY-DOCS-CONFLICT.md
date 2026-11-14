# LE-APPEND-ONLY-DOCS-CONFLICT append audit

Audited the reachable non-merge history with `git log -p --since=2026-11-01 -- CHANGELOG.md docs/SPAWNER.md CONTRIBUTING.md docs/KEEPER.md` on base `6e43f27f` (2026-11-14). Merge commits are excluded from the counts to avoid counting inherited entries twice. With `--unified=0`, count insertion-only hunks (old count zero); an EOF append inserts at the parent's line count. Repeated old line coordinates are a reproducible proxy for same-place insertion, not proof of overlapping branches.

| File | Insertion-only hunks | EOF appends | Most repeated old insertion coordinates (line: count) |
| --- | ---: | ---: | --- |
| `CHANGELOG.md` | 69 | 0 | 6: 56, 5: 10, 9: 1 |
| `docs/SPAWNER.md` | 62 | 14 | 725: 5, 1: 5, 696: 3 |
| `CONTRIBUTING.md` | 59 | 22 | 349: 5, 307: 4, 320: 3 |
| `docs/KEEPER.md` | 6 | 1 | 427: 1, 145: 1, 24: 1 |

The contextual diff also has 81 CHANGELOG hunks at the opening anchor `Merged and pushed changes only`. Ten checkpoint-addition lines occur in SPAWNER/CONTRIBUTING (including the earlier checkpoint 8 text later replaced). The nine current records are moved into `PHASE-LINE-GREEN-1-<checkpoint>.md` here. KEEPER has no matching checkpoint records; substantive reference documentation remains in place. All three reference docs link this directory rather than maintaining a per-PR list. Future status and checkpoint notes use `<ITEM>.md`, while normal reference edits continue in their subject sections.

`CHANGELOG.md` remains the sole union ledger for historical lanes and release folding. Ordinary PRs add unique changelog fragments and unique checkpoint records. The fixture batch tests both this workflow and legacy same-place changelog insertion, with the union defaults copied from the fetched base's `.gitattributes` into saved batch state.
