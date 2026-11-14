# Keeper hygiene reports and cancellation dispositions

`pg-ctx lint SNAPSHOT DECISIONS UNIX-SECONDS` emits a JSON report. Classes are
`stale>30d`, `title-twin` (normalized title within a project), and
`dead-D-citation` (a referenced decision absent from the authority export).
Only nonterminal items are reported. Terminal twins remain comparison evidence.
Age uses the newest parseable update/activity time; exactly 30 days is not stale.
Missing dates do not become age evidence. Lint never changes or cancels items.

`pg-ctx lint-now SNAPSHOT DECISIONS OUTPUT` atomically publishes the same report
using the current clock. The supplied daily user timer writes `status.json`
under its state directory. Configure `SNAPSHOT` and an origin-backed `DECISIONS`
export in the service environment. The external routerctl owner must read that
file into daily status. Timer installation and routerctl changes are not made in
this repository/worktree.

`pg-ctx cancel-check SNAPSHOT DISPOSITIONS REPOS DECISIONS UNIX-SECONDS D-ID`
validates a proposed batch and emits a keeper-attachable JSON record containing
one D-id and every per-item disposition. `REPOS` is the full routing-owned list
of repository identifiers, as a JSON string array. `DISPOSITIONS` is an array of
records with these required fields (no omitted/unknown fields):

* `key`, `item_updated_at`, `checked_at` (Unix seconds), `checked_repos`,
  `run_window_start` (at least 14 days before the check), and `premise_checked`;
* `claims`, `runs`, `open_prs`, `branches`, `lanes`, `merged_commits`,
  `dependents`, `gate_edges`, `operator_flags`: all must be known empty arrays;
* `reason`: either `{ "kind":"premise_superseded", "rule":"9.14" }`, or
  `{ "kind":"twin", "key":"project:TWIN", "dc_covers":true,
  "unique_dc_moved":true }`.

The item must still be open, have `operator:false`, and match the checked update
revision. Checks expire after an hour. Every configured repository must have
been checked; unknown evidence refuses. The routing row must occur exactly once
and its DECISION cell must begin `CANCEL key1 key2; rationale`. Fenced and
indented examples do not authorize cancellation. Every proposed key must be in
that row; classes alone confer no authority.

Disposition assertions are supplied by the routing-owned collector; this tool
cannot prove absence across repositories it cannot access or semantic DC
coverage from booleans. Archive the checked record with the routing decision.
This is a report/validation boundary, not a replacement for keeper authorization.
The existing generic keeper cancel endpoint is unchanged; universal enforcement
there, attachment of these records to keeper events and the all-repository
collector remain integration work. This package does not claim those live or
cross-project acceptance criteria are complete.
