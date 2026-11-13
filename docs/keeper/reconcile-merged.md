# Reconcile merged work

`pg-keeper-truth reconcile-merged SNAPSHOT INDEX [ASSESSMENTS]` is a file-only
dry run and emits TSV: key, state, proposed action, PRs, commit count, last date,
and proposed note. Use a snapshot containing one project's items and the index
from `pg-keeper-truth index REPOSITORY origin/main`. Repeat for every repository.
Every nonterminal item with an exact Plan-Item match is listed; terminal items
and unmatched items are excluded. Missing assessments propose CHECK.

Assessments are a JSON object keyed by full item key. Each value contains:
`all_slices_merged` (boolean), the exact current `done_criteria` string, the
complete `commits` SHA list from the index, positive `prs`, recorded reviewer
`families`, and a nonblank `reason` explaining coverage or remaining slices.
The tool refuses stale/incomplete assessments as CHECK. A complete assessment
proposes CLOSE when all slices are covered, otherwise SLICE-OPEN, with merged
PRs and commits in the proposed note. Review that note before applying an edit.
PR associations in assessments are operator evidence; the tool does not infer
done-criteria coverage or PR provenance from the existence of a commit.
The index also lists PR numbers from the first mainline merge introducing each
commit (or a conventional squash subject). Its graph walk processes each commit
once; arbitrary issue references in subjects are not treated as PR numbers.

Only `--apply D-NNN` calls `pg close-merged KEY D-NNN --family FAMILY` for CLOSE
rows. Every row uses the same decision id. All rows are prevalidated before any
call; open/claimed CLOSE rows refuse the batch. The existing keeper verb remains
the authority for GO-head binding, state, seats and writer independence; it
records verified decision and merged-head evidence. The TSV retains the PR
association and rationale. No database writes or alternative close path exist.
Calls stop on the first refusal; refresh the snapshot before retrying a partly
applied batch. The batch is not an atomic multi-item keeper transaction.

The existing close verb accepts one GO head per decision. A multi-head batch
needs a router ruling compatible with that proof contract before it can close;
this tool does not broaden that authority. Likewise open/claimed rows need
router handling through the existing lifecycle before closure. These are
reported limits, not automatic verdicts or automatic legacy-state transitions.

The supplied user timer templates run `monitor SNAPSHOT REPOSITORY REF STATE`
hourly. Configure `SNAPSHOT`, `REPOSITORY` and `DEFAULT_REF` in the instance's
environment file. The snapshot must have been refreshed within two hours.
Monitor refreshes the repository index, lists TSV to the journal, and emits one
`ROUTING ALERT` to stderr after a continuously nonzero count exceeds one day.
Persisted state survives restarts, resets on zero, and refuses corrupt state.
The operator's journal-to-routing collector must consume this alert. Timer
installation, live snapshot publication, routing hookup and historical backfill
are deployment work for the orchestrator; none were performed in this worktree.
