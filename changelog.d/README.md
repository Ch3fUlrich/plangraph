# Changelog fragments

Add one entry line per PR as `changelog.d/<ITEM-KEY>.md` (several parts of one item: `<ITEM-KEY>-<n>.md`); `tools/changelog_fold.py` folds them under `## Unreleased` and deletes them.

Fragments must be direct children, not nested files; README.md (any case) is excluded. New or edited fragments must add an entry line against the merge-base; whitespace changes and removals do not count.

Fold orders uncommitted fragments first by newest mtime, then committed fragments by newest first-commit date, with filename ties. Only fragments with folded entry lines are deleted. Empty, whitespace-only and heading-only fragments are retained and reported. `--check` writes nothing and exits 1 when fragments remain; normal folds exit 0.
