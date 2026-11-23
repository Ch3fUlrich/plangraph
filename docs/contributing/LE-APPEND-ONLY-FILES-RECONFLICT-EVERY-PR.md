## Per-item documentation fragments

When a change needs an update to a shared reference, add a Markdown file
named after the Keeper item ID, without the project prefix. Use the folder
that corresponds to the mapped reference.

| Reference | Fragment folder |
| --- | --- |
| `CONTRIBUTING.md` | `docs/contributing/` |
| `docs/KEEPER.md` | `docs/keeper/` |
| `docs/SPAWNER.md` | `docs/spawner/` |

The filename starts with an uppercase ASCII letter and may also contain
digits, underscores and hyphens. Both docs-map gates accept a newly added
fragment in the matching folder. Corrections to an existing reference can
still edit that reference directly.

`LE-DOCS-FRAGMENT-LANDING-FOLD` in the batch's `slices.json` tracks the
main-only generator, a two-branch fixture that merges independent item
fragments and checks the folded document, and the legacy allowlist parity
test. The docs-map acceptance tests cover each of the three fragment
folders today.
