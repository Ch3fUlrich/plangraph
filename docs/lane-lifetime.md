# Lane branch and probe pull request lifetime

`plangraph lane-cleanup` (code: `rust/plangraph-cli/src/land/lane_cleanup.rs`, `lane_probe.rs`; the
delete door: `rust/worker-core/src/lane_delete.rs`) gives every remote `lane/*` head and every CI probe
pull request an end of life. Daily run: `deploy/systemd/pg-lane-cleanup.{service,timer}`.

## Classes

| Class | Proof (against a fresh `origin/main` fetched in the same run, for the exact head SHA) | Action |
| --- | --- | --- |
| merged | head is an ancestor of main | delete |
| in main by patch | no merge commit in `main..head` and every commit has a patch-id match in main | delete |
| superseded | a replacement record (below) whose two refs still sit at the recorded SHAs on the remote, same lane stem, later round, and every old commit is in the new head or in main | delete |
| stale | older than `--stale-days` (3) and none of the above | report only |
| live | everything else | keep |

Kept even when eligible: a claim of the keeper claims reader, an open pull request that has the ref as
head **or base** (an open child of a closed parent keeps the parent), a local checkout, a ref listed in
`<state>/lane-holds.txt` (settlement holds the ref until its writer-stop step ends), and any run where
the claims reader, the open-PR inventory (at most 999 rows) or the repository binding (`--remote` URL ends
in `--gh-repo`) is unresolved. The run ends with one `reconcile heads=… eligible=… protected=…
deleted=… absent=… moved=… refused=… stale_report_only=… remaining=…` line.

## Replacement and probe records (producers)

- `plangraph lane-cleanup record-replacement --batch-key K --old REF:SHA --new REF:SHA` checks both refs
  on the remote, a later round of one stem and the preservation proof, then appends
  `<state>/lane-replacements.jsonl`. A higher `-rN` alone never deletes: without a record the head stays.
- `plangraph lane-cleanup record-probe --gh-repo R --pr N --head SHA --run-id ID` appends
  `<state>/probe-prs.jsonl`; `close-probes` closes a PR only when its record exists, the PR is open at
  the recorded head and the run (same head) is `completed`. Ordinary drafts have no record.

## Authority (rollout prerequisite)

The delete is a push from `PG_LANE_DELETE_REMOTE`. Before `--apply` is enabled the operator must:
1. install the land App on the repository with permission to delete `refs/heads/lane/*` and add a
   destination ruleset that allows only that App to delete `lane/*` (the `main` rules stay unchanged);
2. give the host a remote that authenticates as that App and mark it:
   `git config remote.<name>.plangraph-lane-delete app`.

The repository's `tools/hooks/pre-push` runs on this push (no `core.hooksPath` override) and refuses a
delete unless the remote carries that mark, the path in `PLANGRAPH_LANE_DELETE_RECORD` holds the exact
ref and SHA, and `refs/plangraph/restore/<sha>` pins the commit. `pg-worktree-sweep --delete-remote`
uses the same door for `lane/*` refs.

## Restore and retry

Each delete first pins the commit as `refs/plangraph/restore/<sha>` and syncs a row to
`<state>/sweep/<ts>.tsv` (repo, `-`, ref, sha). Restore: `git push <remote> refs/plangraph/restore/<sha>:refs/heads/lane/<name>`.
The push uses `--force-with-lease=<ref>:<sha>`. Outcomes: deleted and absent are done; moved
(the remote advanced) is kept and re-evaluated on the next run; refused (hook, ruleset, network) exits 3,
so a strict settlement step fails and resumes, and the daily timer retries.
