# Lane branch and probe pull request lifetime

`plangraph lane-cleanup` (code: `rust/plangraph-cli/src/land/lane_cleanup.rs`, `lane_proof.rs`,
`lane_repo.rs`, `lane_probe.rs`; the delete door: `rust/worker-core/src/lane_delete.rs`) gives every
remote `lane/*` head and every recorded CI probe pull request an end of life. Daily run:
`deploy/systemd/pg-lane-cleanup.{service,timer}`.

## Classes

| Class | Proof (against a `main` fetched in the same run, for the exact head SHA) | Action |
| --- | --- | --- |
| merged | head is an ancestor of main | delete |
| in main | `git merge-tree --write-tree main head` is clean and yields main's own tree: merging the head changes nothing (whitespace exact, merge commits covered; patch ids are not used because they ignore whitespace) | delete |
| superseded | a replacement record (below) whose two refs still sit at the recorded SHAs on the remote, same lane stem, later round, and the old head is an ancestor of the new head or adds nothing to the new head or to main | delete |
| stale | older than `--stale-days` (3) and none of the above | report only |
| live | everything else | keep |

Kept even when eligible: a claim of the keeper claims reader (`branches`, plus the branch of any local
worktree under a claimed `path`), a local checkout, a ref in `<state>/lane-holds.txt`, an open pull
request that has the ref as head **or base** (read for that ref right before the push), and any run where
the claims reader, the PR read or the repository binding is unresolved. When the claims reader reports
`unbound_runs` (active runs of other hosts have no local record naming their publish ref) only the merged
class is deleted. The run ends with one `reconcile heads=… eligible=… protected=… deleted=… absent=…
moved=… refused=… stale_report_only=… remaining=…` line.

Repository binding: the fetch URL and the effective push URL (`pushurl` and `insteadOf` applied) of
`--remote` must both reduce to `--gh-host`/`--gh-repo` (default host `github.com`; `--gh-host local` is
for offline fixtures), and pull request reads use `gh --repo HOST/OWNER/NAME`.

## Replacement and probe records (producers)

- `plangraph lane-cleanup record-replacement --batch-key K --old REF:SHA --new REF:SHA --gh-repo R`
  checks the repository, both refs on the remote, a later round of one stem and the preservation proof,
  then appends `<state>/lane-replacements.jsonl`. A higher `-rN` alone never deletes.
- `plangraph lane-cleanup record-probe --gh-repo R --pr N --head SHA --run-id ID` appends
  `<state>/probe-prs.jsonl`; `close-probes` closes a PR only when its record exists, the PR is open at the
  recorded head and the run (same head) is `completed`, read twice, the second time right before the close.
  Ordinary drafts have no record. No code of this repository creates probe PRs, so the caller of
  `record-probe` is whatever creates them; follow-up slice: wire both record producers.

## Authority (rollout prerequisite)

The delete is a push from `PG_LANE_DELETE_REMOTE`. Before `--apply` is enabled the operator must:
1. install the land App on the repository with `Contents: write` and a destination ruleset
   (`Settings > Rules > Rulesets`, target `refs/heads/lane/**`, enforcement `active`) that restricts
   deletions (`Restrict deletions` on) with the land App as the only bypass actor, then record the
   installation id and the ruleset id/name in the rollout record; the `main` rules stay unchanged;
2. give the host a remote that authenticates as that App and mark it:
   `git config remote.<name>.plangraph-lane-delete app`;
3. create `~/.config/plangraph/lane-cleanup.env` from `deploy/systemd/lane-cleanup.env.example`, then
   `systemctl --user enable --now pg-lane-cleanup.timer` and record `systemctl --user list-timers
   pg-lane-cleanup.timer` as the activation evidence;
4. run `plangraph lane-cleanup ... --apply` once by hand and keep its `reconcile` line and
   `git ls-remote origin 'refs/heads/lane/*' | wc -l` as the first-run evidence.

The repository's `tools/hooks/pre-push` runs on this push (no `core.hooksPath` override) and refuses a
delete unless the remote carries that mark, the path in `PLANGRAPH_LANE_DELETE_RECORD` holds the exact
ref and SHA, and `refs/plangraph/restore/<sha>` pins the commit. `pg-worktree-sweep --delete-remote`
uses the same door for `lane/*` refs; both check open PR heads and bases for the ref right before the push.

## Restore and retry

Each delete first pins the commit as `refs/plangraph/restore/<sha>` and syncs a row to
`<state>/sweep/<ts>.tsv` (repo, `-`, ref, sha). Restore: `git push <remote> refs/plangraph/restore/<sha>:refs/heads/lane/<name>`.
The push uses `--force-with-lease=<ref>:<sha>`. Outcomes: deleted and absent are done; moved
(the remote advanced) is kept and re-evaluated on the next run; refused (hook, ruleset, network) exits 3,
so a strict settlement step fails and resumes, and the daily timer retries. Settlement passes only the
frozen included refs and SHAs of parts whose pull request head is a branch of this repository.
