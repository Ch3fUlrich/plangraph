# CI runs cancelled on land/* branches, by cause

One-off analysis (no workflow change) of the `tests` runs of `land/*` branches created since
2026-11-21 19:00Z, read from the GitHub API on 2026-11-24. `status=cancelled` returns 36 runs on
`land/*` branches (41 on all branches); the table has one row for each of the 36, all with event `pull_request`.
Cancellation time is the latest cancelled-job end time; "run update time" marks runs with none.
The cause is the run that started after the cancelled run was created and before its
cancellation: a different head commit means a newer push, the same head means the pull request was
closed and reopened or edited (`concurrency.cancel-in-progress` is true for `pull_request` events
other than `labeled`, `.github/workflows/tests.yml`). No run timed out and none was cancelled by hand with no
newer run behind it.

Method: runs of the workflow on `land/*` since the cutoff, each cancelled run matched to the oldest
run on the same branch created within its life (runs created in the same second are ordered by run id); same-head matches checked against the pull
request timeline (`reopened`, `renamed`). A same-head cause with no timeline event is inferred
(a body or base edit leaves no timeline entry).

| run id | event | attempt | branch | head | created | cancelled at | cancelled by run | cause |
|---|---|---|---|---|---|---|---|---|
| 44953045584 | pull_request | 1 | land/train-20261121-8b | 8ea416048e | 2026-11-21T21:43:52Z | 2026-11-21T21:53:37Z | 44953854663 | pull request closed and reopened (same head, new run) |
| 44953854663 | pull_request | 1 | land/train-20261121-8b | 8ea416048e | 2026-11-21T21:53:20Z | 2026-11-21T21:53:23Z (run update time) | 44953856110 | newer push to the same pull request (new head commit) |
| 44953856110 | pull_request | 1 | land/train-20261121-8b | afcfd60d34 | 2026-11-21T21:53:21Z | 2026-11-21T22:03:00Z | 44954648287 | pull request closed and reopened (same head, new run) |
| 44954648287 | pull_request | 1 | land/train-20261121-8b | afcfd60d34 | 2026-11-21T22:02:43Z | 2026-11-21T22:02:45Z (run update time) | 44954649960 | newer push to the same pull request (new head commit) |
| 44973875711 | pull_request | 1 | land/train-20261121-9 | b627322e2a | 2026-11-22T01:58:09Z | 2026-11-22T01:58:10Z | 44973876528 | newer push to the same pull request (new head commit) |
| 44982808388 | pull_request | 1 | land/train-20261121-9 | 06ce16c763 | 2026-11-22T03:56:00Z | 2026-11-22T03:56:01Z | 44982808928 | newer push to the same pull request (new head commit) |
| 44982808928 | pull_request | 1 | land/train-20261121-9 | 16d5f979da | 2026-11-22T03:56:00Z | 2026-11-22T04:01:11Z | 44983151396 | pull request title edited (same head, new run) |
| 44985858180 | pull_request | 1 | land/train-20261121-9 | 16d5f979da | 2026-11-22T04:37:15Z | 2026-11-22T04:37:16Z | 44985858931 | newer push to the same pull request (new head commit) |
| 44985858931 | pull_request | 1 | land/train-20261121-9 | d70cb1c78f | 2026-11-22T04:37:15Z | 2026-11-22T04:57:49Z | 44987286714 | pull request closed and reopened (same head, new run) |
| 44987286714 | pull_request | 1 | land/train-20261121-9 | d70cb1c78f | 2026-11-22T04:57:34Z | 2026-11-22T04:57:35Z (run update time) | 44987287430 | newer push to the same pull request (new head commit) |
| 44991308840 | pull_request | 1 | land/train-20261121-9 | 11e2a509dd | 2026-11-22T05:53:15Z | 2026-11-22T05:53:16Z | 44991310118 | newer push to the same pull request (new head commit) |
| 44991310118 | pull_request | 1 | land/train-20261121-9 | 5577564191 | 2026-11-22T05:53:16Z | 2026-11-22T05:55:21Z | 44991432390 | pull request edited (inferred: same head, no push, reopen or label event) |
| 44993595592 | pull_request | 1 | land/train-20261121-9 | 5577564191 | 2026-11-22T06:24:44Z | 2026-11-22T06:24:46Z | 44993596373 | newer push to the same pull request (new head commit) |
| 44993596373 | pull_request | 1 | land/train-20261121-9 | 78a4e3e7d7 | 2026-11-22T06:24:45Z | 2026-11-22T06:24:58Z (run update time) | 44993609936 | pull request closed and reopened (same head, new run) |
| 44999332806 | pull_request | 1 | land/train-20261121-9 | 78a4e3e7d7 | 2026-11-22T07:40:57Z | 2026-11-22T07:40:59Z | 44999334184 | newer push to the same pull request (new head commit) |
| 45001451789 | pull_request | 1 | land/train-20261121-9 | 1a004a1330 | 2026-11-22T08:09:08Z | 2026-11-22T08:09:10Z | 45001452641 | newer push to the same pull request (new head commit) |
| 45012565732 | pull_request | 1 | land/train-20261122-10 | e7831129ac | 2026-11-22T10:27:59Z | 2026-11-22T10:28:01Z | 45012567367 | newer push to the same pull request (new head commit) |
| 45014554437 | pull_request | 1 | land/train-20261122-10 | 542a07d68f | 2026-11-22T10:52:46Z | 2026-11-22T10:52:48Z | 45014555195 | newer push to the same pull request (new head commit) |
| 45017811956 | pull_request | 1 | land/train-20261122-10 | 6bc1d67b0e | 2026-11-22T11:32:16Z | 2026-11-22T11:32:18Z | 45017812797 | newer push to the same pull request (new head commit) |
| 45017812797 | pull_request | 1 | land/train-20261122-10 | 8957f3162d | 2026-11-22T11:32:17Z | 2026-11-22T11:34:13Z | 45017948062 | pull request title edited (same head, new run) |
| 45017948062 | pull_request | 1 | land/train-20261122-10 | 8957f3162d | 2026-11-22T11:33:57Z | 2026-11-22T11:54:50Z (run update time) | 45019559552 | pull request closed and reopened (same head, new run) |
| 45019559552 | pull_request | 1 | land/train-20261122-10 | 8957f3162d | 2026-11-22T11:54:03Z | 2026-11-22T11:54:05Z (run update time) | 45019561304 | newer push to the same pull request (new head commit) |
| 45035566659 | pull_request | 1 | land/train-20261122-11 | fa6ff049c2 | 2026-11-22T14:56:57Z | 2026-11-22T14:56:59Z | 45035567759 | newer push to the same pull request (new head commit) |
| 45037550746 | pull_request | 1 | land/train-20261122-11 | 26de385619 | 2026-11-22T15:18:18Z | 2026-11-22T15:18:20Z | 45037552042 | newer push to the same pull request (new head commit) |
| 45040594797 | pull_request | 1 | land/train-20261122-11 | 7e3cf0432e | 2026-11-22T15:51:27Z | 2026-11-22T15:51:29Z | 45040596549 | newer push to the same pull request (new head commit) |
| 45040596549 | pull_request | 1 | land/train-20261122-11 | ce2928e8d2 | 2026-11-22T15:51:28Z | 2026-11-22T15:54:16Z | 45040822086 | pull request edited (inferred: same head, no push, reopen or label event) |
| 45052513651 | pull_request | 1 | land/train-20261122-11 | ce2928e8d2 | 2026-11-22T17:59:47Z | 2026-11-22T17:59:49Z | 45052515578 | newer push to the same pull request (new head commit) |
| 45075114420 | pull_request | 1 | land/train-20261122-12 | 24ee0b9747 | 2026-11-22T21:59:21Z | 2026-11-22T22:12:06Z | 45076350282 | pull request closed and reopened (same head, new run) |
| 45076350282 | pull_request | 1 | land/train-20261122-12 | 24ee0b9747 | 2026-11-22T22:11:53Z | 2026-11-22T22:11:55Z (run update time) | 45076351583 | newer push to the same pull request (new head commit) |
| 45076351583 | pull_request | 1 | land/train-20261122-12 | 8e18cac1d2 | 2026-11-22T22:11:54Z | 2026-11-22T22:18:52Z | 45076986516 | pull request closed and reopened (same head, new run) |
| 45076986516 | pull_request | 1 | land/train-20261122-12 | 8e18cac1d2 | 2026-11-22T22:18:34Z | 2026-11-22T22:18:37Z (run update time) | 45076988757 | newer push to the same pull request (new head commit) |
| 45080471517 | pull_request | 1 | land/train-20261122-12 | 94689d1c79 | 2026-11-22T22:56:23Z | 2026-11-22T22:56:25Z | 45080473403 | newer push to the same pull request (new head commit) |
| 45080473403 | pull_request | 1 | land/train-20261122-12 | 970ca0e832 | 2026-11-22T22:56:24Z | 2026-11-22T22:56:39Z (run update time) | 45080492526 | pull request closed and reopened (same head, new run) |
| 45080492526 | pull_request | 1 | land/train-20261122-12 | 970ca0e832 | 2026-11-22T22:56:38Z | 2026-11-22T23:07:38Z | 45081496720 | newer push to the same pull request (new head commit) |
| 45161902544 | pull_request | 1 | land/train-20261123-17 | 11db4f3286 | 2026-11-23T11:05:06Z | 2026-11-23T11:13:26Z | 45163165185 | newer push to the same pull request (new head commit) |
| 45224575341 | pull_request | 1 | land/train-20261123-20b | fb58d3de88 | 2026-11-23T17:09:45Z | 2026-11-23T17:10:08Z | 45224606941 | pull request edited (inferred: same head, no push, reopen or label event) |

| cause | runs |
|---|---|
| newer push to the same pull request (new head commit) | 23 |
| pull request closed and reopened (same head, new run) | 8 |
| pull request edited (inferred: same head, no push, reopen or label event) | 3 |
| pull request title edited (same head, new run) | 2 |
| total | 36 |

The "newer push" rows (23) confirm the likely cause. Thirteen runs have a different one: a train pull request was closed and reopened
(or edited) on the same head, which starts a run that cancels the one in flight. Who closes and reopens is
not read from the data (one account does every action). A follow-up item covers it (CI-REOPEN-CANCELS-LANDING-RUN in the batch slices).
