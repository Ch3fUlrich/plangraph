# Backtracking via Changelogs and ADRs

The goal: any future agent (or human) can answer "why is the code like this?" and
"how do I safely undo it?" from the repository alone, without the original
author.

Two complementary records:

1. **`CHANGELOG.md`** — *what* changed, in human terms, over time.
2. **`docs/decisions/`** (ADRs) — *why* a non-obvious choice was made.

Plus the discipline that makes both usable: **small, frequently committed,
independently revertible changes**.

## CHANGELOG.md

Follow [Keep a Changelog](https://keepachangelog.com/) conventions. Newest entry
on top. Group by `Added / Changed / Fixed / Removed / Deprecated / Security`.

```markdown
# Changelog

## [Unreleased]

### Removed
- The polling fallback of the job queue, entirely — events are the only trigger. The
  escape hatch was never exercised but kept leaking back into docs as a live option.
  See ADR 0003 (remove the polling fallback).

### Added
- `coding-principles` skill.
```

Rules:
- One bullet per meaningful change; reference the ADR or commit where relevant.
- Write it in the same change that makes the code change — not later.
- User-facing language: describe the effect, not the diff.
- In plangraph the entry is a fragment, `changelog.d/<ITEM-KEY>.md` holding one entry line
  (see `changelog.d/README.md`); `tools/changelog_fold.py` folds fragments under `## Unreleased`.

## Architecture Decision Records (ADRs)

For any decision that a reasonable engineer might later question — a dependency
choice, a tradeoff, a rejected alternative — write an ADR. Keep them short.

Path: `docs/decisions/NNNN-kebab-title.md` (zero-padded, monotonic). The ADR file names in the
examples below are illustrative; they are not files in this repository.

```markdown
# 0001. Event triggers over polling for the job queue

- **Status:** Accepted (2026-07-09)
- **Context:** Jobs start from a cron poll every minute. Latency is up to a minute and
  every poll reads the full table; we want a start on the event that makes work ready.
- **Decision:** Adopt event triggers as the default. Replace the poll loop with a
  handler per event type.
- **Consequences:** A missed event now needs a recovery path (treated as a feature:
  the handler is idempotent and replayable). Polling kept as fallback.
- **Switch-back criteria:** If events prove unreliable, re-enable the `polling-fallback`
  setting and layer the handlers on top.
```

**Amend, don't rewrite.** When reality moves on, an ADR is a record of what was decided
*then* — so leave the text and add a dated note pointing at the ADR that changed it. The
example 0001 above later had its fallback clause voided; the fix was a header amendment plus
a `> Superseded by ADR 0003` note above that section — **not** a silent edit. Write the
new ADR for the new decision (0003 here), and keep the old one findable:

```markdown
# 0001. Event triggers over polling for the job queue

- **Status:** Accepted (2026-07-09)
- **Amended (2026-07-16):** the polling fallback below was removed — see
  ADR 0003 (remove the polling fallback). The decision to adopt event triggers
  stands; only the escape hatch is gone.
```

The same rule applies to memory: supersede an accepted `Decision` with a new node plus a
`Supersedes` edge rather than overwriting its rationale (`skills/structured-memory/SKILL.md`).

Statuses: `Proposed → Accepted → Superseded by NNNN → Deprecated`. Never edit the
decision of an accepted ADR — supersede it with a new one and link both ways.

## Commit discipline (what makes reverts possible)

- One logical change per commit; each commit leaves the tree working.
- Message states **intent**: `feat: default memory to Omnigraph` not `update files`.
- Prefer many small commits over one large one — a bad change should be a single
  `git revert`, not archaeology.
- Reference the ADR/changelog entry in the body when the change is a decision.
