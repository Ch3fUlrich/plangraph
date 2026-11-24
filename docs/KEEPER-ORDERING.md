# Keeper: which item may be worked on, and in what order

Part of [KEEPER](KEEPER.md); the verbs and the schema are described there.

**Superseded and duplicate items (ITEM-SUPERSEDE-GATE).** `supersedes` (item keys, editable, column since schema v28) retires the named
items: every claim role refuses them, again under the claim transaction (`superseded by KEY`, not overridable by `force_stale`; a done
superseder keeps them retired, a cancelled one frees them), and `ready` lists them under `needs_reconcile`, so the feeder drops them, as it
does items whose cited decision was retired (`superseded_premises`, `superseded_contract`; checked for every claim role). A link follows a
rehome alias, may not close a cycle of any length, and to another project's item (also through a grant, a `similar` answer or `apply_ops`)
is the router's. `reconfirm KEY D-NNN --supersession SUPERSEDER` clears exactly that pair for the link as it stands (audited; a link removed
and added again is a new one); cycle checks resolve aliases too.
Every create path (`define`, `define --edit` of a title or description, `apply_ops`/md-sync, plan import) runs one similarity check
(`item_fields.similar_items`: token overlap of title 60% and description 40%, files when both name them, titles of 3+ words, threshold 0.6, an exact
title+description copy at any length, same project, open items, batch and import peers (an export carries each item's recorded answers, a removed link's answer as `distinct`; a pair answered by either side counts), slices of one parent excepted) and refuses a near copy with the matching keys until each is answered
`similar={KEY: supersedes|depends_on|distinct}` (md-sync: a `Similar: KEY=answer, ...` token ending the step cell; YAML plans: a `similar`
mapping); the answer is stored in the event. EXPERIMENT (rule 3.76): `reap`-independent day counters `similar_matches_shown`,
`similar_answered.<answer>` count matches shown and answers (not for a dry run); a later `supersedes` edit by hand is the duplicate found afterwards.

**Premise precision (KEEPER-STALE-PREMISE-PRECISION).** The premise gate ignores a newer decision that (a) is the item's own `source`, (b) is a
terse order: after the stamp every token is the item's key or bare id, a D-id or an ordering word (go, order, define, queue ...), and no
scope or condition word occurs, so "GO x; use Postgres", "GO x; X=0" and "GO x; backend:postgres" are left to a human, or (c) names the
session that defined the item (`define ... session=ID`, kept in the creation event) and carries a full timestamp (`2026-11-24T08:34Z`)
within two hours after the define, without a scope word. The log's plain `(HH:MMZ` stamps never reach (c). `reconfirm KEY D-NNN` records, as an
audited `premise_reconfirmed` event, that the premise was read against that decision: it clears `newer_decisions` and retired premises up
to it (system leases too); an explicit `supersedes` link is cleared only with `--supersession SUPERSEDER`. `alerts` reports
`force_stale_claims`, counted from the audited claims of the alert window.

**Effective priority (PRIORITY-INHERIT-FROM-BLOCKED).** `views.effective_priorities` gives every non-terminal item max(own, effective of each
non-terminal item that depends on it), transitively (a cycle is reported, its back edge ignored); the stored priority never changes and
aging/`priority` act on it alone. `ready`, the feeder pick (`effective_priority` on the ready row) and the web table sort by the effective
value and show `8->10 via KEY`; the snapshot carries `effective_priority`/`priority_via` for raised items only. `reap` upserts the day's
metrics `priority_inherit_raised` and `priority_inherit_longest_chain`.

**Categories (ITEM-CATEGORY-RANKED, schema v29).** `items.category` (set at define, editable, null = none) names a row of the `categories`
table: project `*` holds the default ranked list (seed: speed, efficiency-tokens, efficiency-quality, bug-fix, reliability, telemetry, security,
network, planning, ui-ux, feature, cleanup); `add-category NAME` appends one, `rank-categories PROJECT NAME...` gives a project its own order
(a category added later is appended to it), `categories [--project]` reads the list and the uncategorised count, `backfill-categories PROJECT
[--dry-run]` sets OPEN uncategorised items by key/track rules, names skipped and failed items, and returns the unmatched keys for a model pass.
`ready`, the feeder (`category_rank` on the ready row) and the web table order by category rank, then effective priority, then age (oldest
first), after the feeder's classes (live PR, CI fix: `work_class`); an item without category sorts last. The snapshot always carries `categories` and `uncategorised` (block `categories`), and
`category_overrides` for projects with their own order. `reap` records `category_ready.NAME` (the keeper's own ready list) and `category_merged.NAME` (verified merges,
explicit zeros) per day and raises `category_starved` on the oldest ready item of a category that has waited over 48 h since its last
dependency was done, with no verified merge. The web page is read-only (it holds no keeper token): its reorder buttons re-sort ONE chosen project's rows at once and show the
`rank-categories PROJECT ...` command that stores the order; a reload then shows the stored order. `copy-to-pg` replaces the target's seed rows with the source's.
