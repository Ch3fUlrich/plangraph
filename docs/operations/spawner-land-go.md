# Land a batch on a GO row

`pg-spawnd land-go --rows FILE --done FILE --repo OWNER/NAME --checkout DIR --post-cmd CMD
[--keeper-cmd CMD]` is one pass over the router's GO rows; a timer runs it, and the trigger is a
GO row for an exact batch head. A row is one line with `GO`, `D-<id>`, the batch pull request
(`#<n>`), the full head and optional `plangraph:<KEY>` item keys. For each row not yet in the
done file the pass checks that the head GitHub shows is the row's head, that it is 0 commits behind
main and that the required checks are green, then runs `tools/merge_batch.py land` with the row as
approval, posts `MERGED <sha> #<n> <head> <D-id>` through the post command and runs `close-merged
KEY D-<id> --from-open` as a dry run per key, posting its result or refusal. A moved head, a stale
base, a red check or a refused land posts `BLOCKED land <D-id> #<n> <head> | <reason>` and lands
nothing; each `<D-id> <head>` is handled once. Any other trigger does not land.

## Landing modes (`landing.mode`, decision D-6540)

`tools/merge_batch.py land` and the router's `merge-part` read one setting, `landing.mode`, per repository from the
settings file `PLANGRAPH_SETTINGS` names (`[landing] mode = "..."`, `[landing.repos] "owner/name" = "..."`;
`python3 -m plangraph.settings landing-mode --repo owner/name` and `merge_batch.py landing-mode` print it). An unknown
mode or table refuses the land before any call to GitHub. No landing path was deleted:

- `tested-ff` (the recommended mode): after the same gates (approval, part seats, full CI on the exact head, current
  base, required checks, green main) main moves to the batch head, the commit that passed full CI, by one
  `git push --force-with-lease=refs/heads/main:<B> origin <head>:refs/heads/main`. Main moves only while it is exactly
  `B`, the base the batch was tested on; there is no second merge commit and no `gh pr merge`. The outcome is read
  from main: the head in main's history is landed (also after a lost answer or an earlier run: no second push); main
  at another commit is `base_moved`; a refusal by the server is final and keeps the batch; an unknown delivery sends
  the same push again while main is `B`, at most three times. GitHub marks the pull requests merged because their
  heads are in main. A pull request that GitHub shows merged but whose head is not in main (merged another way) is no
  landing in this mode. The batch has no merge commit, so the `Review-Run` citations of the merge body do not exist:
  the receipt keeps them with the batch plan key, the part commits keep their `Plan-Item` trailers, and merge-close
  closes on that merge evidence with the seat flag of the keeper setting `require_seats`. `build --plan KEY` writes the
  batch plan key as a `Plan-Item` trailer into every merge commit it makes, and `tested-ff` refuses a batch whose key
  is not a trailer of a batch commit, and refuses while `require_seats` is on (a tested commit cannot carry the
  `Review-Run` citations of a merge body without changing the tested sha; land in `train-pr-merge` then). The router's
  `merge-part` does the same push from the clone `PG_LAND_CHECKOUT` with the lease on the tested base, which the lander
  records as `base_head` and the router passes in `PG_LAND_BASE` (required; never read from GitHub): it needs the pull
  request to target main, a `ci-full` status by Actions that names a green `pull_request` run of the exact head at its
  latest attempt and the head's tree, green required checks, and main exactly at that base (main at any other
  commit, a commit the head contains included, is
  `base_moved`). Main's protection must accept the push of a commit whose required checks passed (the operator's
  change; the land App design is in the settlement note), so until then the server refuses and the batch stays.
- `train-pr-merge`: the batch pull request is merged with a merge commit (rule 3.39), as before.
- `single-pr`: one pull request merged on its own GO; a batch of several parts is refused.

Until the live proof of `tested-ff` on the real protection passed, `DEFAULT_LANDING_MODE` in
`src/plangraph/settings.py` stays `train-pr-merge`; it becomes the recommended mode by changing that one constant.
Interim rule: `landing.mode` is changed only while the lander is stopped and no batch or landing is open (mode
changes in flight are not built); switching back needs no code change.

The push run on main of a commit the land App authorized after its full run passed (`land-auth` status by the App's bot
user, repository variable `LAND_CREATOR_ID`, and a `ci-full` status of the same run and attempt naming the commit's
tree) runs only the fast jobs (`tools/ci_select.py landing_evidence`); every other push runs the full suite. The full
run of a pull request posts `ci-full` only when the tested tree is the head's tree.

Every landing writes a receipt (`<git dir>/merge-batch/receipts/<train>-<sha>.json`) and one row in the telemetry
database (a landing is identified by repository and landed sha, so a backfill never doubles a recorded landing; `merge-part` writes its row with `merge_batch.py landing-record` and fails the landing, exit 10, when it cannot) (`landing-telemetry.sqlite3` under `$XDG_DATA_HOME/plangraph`, or `PLANGRAPH_LANDING_DB`), both naming the
mode; a landing whose row is missing fails the lander. `merge_batch.py landing-report` compares the modes (trains, median
and worst minutes READY to landed and landed to green, test minutes per part, reruns, red main, fix tasks per train),
`landing-settle` fills the green and red-main columns from GitHub (`--train T --fix-tasks N` records fix tasks) and
`landing-backfill --pr N --mode M` records a train landed before the telemetry existed.
