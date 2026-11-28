# Land a batch on a GO row

`pg-spawnd land-go --rows FILE --done FILE --repo OWNER/NAME --checkout DIR --post-cmd CMD
[--keeper-cmd CMD]` is one pass over the router's GO rows; a timer runs it, and the trigger is a
GO row for an exact batch head. A row is one line with `GO`, `D-<id>`, the batch pull request
(`#<n>`), the full head and optional `plangraph:<KEY>` item keys. For each row not yet in the
done file the pass checks that the head GitHub shows is the row's head, that it is 0 commits behind
main and that the required checks are green, then runs `tools/merge_batch.py land` with the row as
approval, posts `MERGED <sha> #<n> <head> <D-id>` through the post command and runs `close-merged
KEY D-<id>` as a dry run per key, posting its result or refusal. A moved head, a stale
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
  closes on that merge evidence with the seat flag of the keeper setting `require_seats`. `build` writes the batch plan key (`--plan`) as `Plan-Item` and the tested base as `Land-Base: <sha>` trailers into every
  merge commit it makes, so both are part of the reviewed and tested commit. `tested-ff` refuses a batch whose plan
  key is not a trailer of a batch commit, whose head does not carry exactly one `Land-Base` trailer
  (a git trailer, not a body line) naming the recorded base (built by hand, or before the trailer), and refuses unless
  `PG_MERGE_CLOSE_REQUIRE_SEATS` is explicitly `off`, `false` or `0`: the keeper daemon's own `require_seats` cannot
  be read from here, so the choice is explicit, and it is the value the lander's merge-close then sends (a tested
  commit cannot carry the `Review-Run` citations of a merge body without changing the tested sha, so merge-close flags the
  seat as missing; the timer's merge-close follows the daemon's policy). The router's `merge-part` does the same push from the clone
  `PG_LAND_CHECKOUT` with the lease on the base of the head's one `Land-Base` trailer (never GitHub's or the caller's
  word; `PG_LAND_BASE`, when set, must say the same): it needs the pull request to target main, a `ci-full` status by
  Actions that names a green `tests.yml` `pull_request` run of the exact head at its latest attempt and the head's
  tree, green required checks, and main exactly at that base (main at any other commit, a commit the head contains
  included, is `base_moved`). Main's protection must accept the push of a commit whose required checks passed (the operator's
  change; the land App design is in the settlement note), so until then the server refuses and the batch stays.
- `train-pr-merge`: the batch pull request is merged with a merge commit (rule 3.39), as before.
- `single-pr`: one pull request merged on its own GO; a batch of several parts is refused.

Until the live proof of `tested-ff` on the real protection passed, `DEFAULT_LANDING_MODE` in
`src/plangraph/settings.py` stays `train-pr-merge`; it becomes the recommended mode by changing that one constant.
A Keeper-backed landing keeps the mode recorded for its prepared head. Settlement
writes its receipt and telemetry from the journal even when the original checkout
has lost its local state. A legacy manifest uses the saved attempt's mode when
its head matches. Tested fast-forward settlement waits at item closure until
`PG_MERGE_CLOSE_REQUIRE_SEATS` is explicitly off. A prepared attempt whose PR
closed before its head reached main is abandoned.

The push run on main of a commit the land App authorized after its full run passed (`land-auth` status by the App's bot
user, repository variable `LAND_CREATOR_ID`, and a `ci-full` status of the same run and attempt naming the commit's
tree) runs only the fast jobs (`tools/ci_select.py landing_evidence`); every other push runs the full suite. The full
run of a pull request posts `ci-full` only when the tested tree is the head's tree.

Every landing writes a receipt (`<git dir>/merge-batch/receipts/<train>-<sha>.json`) and one row in the telemetry
database (a landing is identified by repository and landed sha, so a backfill never doubles a recorded landing, and a file keyed by train (an earlier draft) is refused and left untouched, never guessed at; reports open a current file without a write lock; `merge-part` writes its row with `merge_batch.py landing-record` and fails the landing, exit 10, when it cannot) (`landing-telemetry.sqlite3` under `$XDG_DATA_HOME/plangraph`, or `PLANGRAPH_LANDING_DB`), both naming the
mode; a landing whose row is missing fails the lander. `merge_batch.py landing-report` compares the modes (trains, median
and worst minutes READY to landed and landed to green, test minutes per part, reruns, red main, fix tasks per train),
`landing-settle` fills the green and red-main columns from GitHub (`--train T --fix-tasks N` records fix tasks) and
`landing-backfill --pr N --mode M` records a train landed before the telemetry existed.

## Where landing time goes (measured 2026-11-28, 100 merged PRs of 2026-11-21 to 11-28)

Source: `gh pr list --state merged`, the pull request commits and `gh run list`; the head's last push is its last commit time.

- Last head commit to merge: median 56 min, p90 163 min (all 100). Trains 36 / 64 min (41 PRs, 40 of them before 11-26); one-by-one merges 86 / 176 min before 11-26 and 62 / 128 min after.
- Re-merges (a commit with two parents that merges main into the head): 292 over 100 PRs, 58 PRs had one or more. One-by-one PRs after 11-26: 2.8 per PR (18 PRs); train PRs: 5.9 per PR (one had 44).
- From the last non-merge commit to merge, one-by-one PRs after 11-26: median 138 min, which is the re-merge cycles, not the GO.
- Per cycle: workstation fast checks 1915-2325 s, then full CI 15.9 min median (p90 17.9, `pull_request`; the `push` run on main is 15.8 min too), then the GO. Every main move starts the cycle again for every open head.
- GO row on data main to merge: 27-29 min for #1336, #1335, #1334, #1332 (merges land at :15 and :45 past the hour), 4 and 11 min for #1331 and #1333. A GO voided by a main move (#1331, #1332) costs one more cycle and one more GO row.
- Order of waits, per landed single PR: BEHIND re-merge cycles (fast checks plus CI, repeated), fast-checks receipt, CI, GO-REQ to GO (not timestamped in data main; the GO commit follows the request by minutes to hours), GO to merge tick; train build and push are small next to these.
- `tested-ff` does not remove the race: its push still needs main to be exactly the tested base (`base_moved` otherwise). A GitHub merge queue tests the speculative merge of main and the head, so no author re-merge happens, but a GO binds the head sha, so it needs the GO check to read the queued head.
