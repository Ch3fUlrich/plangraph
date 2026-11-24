# LE-PROBE-BUDGET-POST-LANDING-EVIDENCE

Item: `plangraph:LE-PROBE-BUDGET-POST-LANDING-EVIDENCE`. Named test file: `tests/test_leg_probe_budget.py`
(split out by commit `29a8edb06`, on main 2026-11-24). Source: `gh api` of the hosted `tests` workflow, runs
created from 2026-11-24T04:00Z, read at the end of 2026-11-24.

## Eligibility

A run is eligible when its head contains `29a8edb06`, its `select` job printed `full=true` (the full
`pytest -q tests` command runs; an affected-selection run is not provable from its quiet log), and all three
`Python tests, SQLite keeper` shard jobs finished `success`. The file runs in exactly one of the three
shards; the shard is not named in the quiet log, so all three job ids are listed. A shard failure would
make the run ineligible; none occurred. Reruns (`attempt` above 1) count as the one run id.

## Runs (13 eligible)

| run id | attempt | kind | shard job ids (1, 2, 3) | runner ids | result |
|---|---|---|---|---|---|
| 45338475207 | 1 | train | 138256164208, 138256164314, 138256164322 | 1000061974, 1000061977, 1000061979 | success |
| 45340810098 | 1 | train | 138263828149, 138263828144, 138263828115 | 1000061989, 1000061990, 1000061995 | success |
| 45344565373 | 2 | main push | 138292719990, 138292720128, 138292720028 | 1000062343, 1000062346, 1000062344 | success |
| 45347547985 | 1 | lane | 138287275331, 138287275310, 138287275360 | 1000062264, 1000062265, 1000062259 | success |
| 45352377896 | 1 | lane | 138301863362, 138301863960, 138301863318 | 1000062405, 1000062406, 1000062404 | success |
| 45365578178 | 1 | train | 138346388054, 138346388142, 138346387975 | 1000062716, 1000062721, 1000062714 | success |
| 45365882966 | 1 | train | 138347516098, 138347516204, 138347516059 | 1000062731, 1000062735, 1000062729 | success |
| 45370719249 | 1 | train | 138364129868, 138364129796, 138364129831 | 1000062755, 1000062748, 1000062751 | success |
| 45376229123 | 1 | main push | 138383290919, 138383290858, 138383290872 | 1000062784, 1000062779, 1000062781 | success |
| 45376508081 | 1 | train | 138384207088, 138384207031, 138384207009 | 1000062803, 1000062799, 1000062795 | success |
| 45376763871 | 1 | train | 138385141314, 138385140983, 138385140981 | 1000062820, 1000062815, 1000062812 | success |
| 45382096214 | 1 | main push | 138403557916, 138403557884, 138403557932 | 1000062833, 1000062837, 1000062836 | success |
| 45382435951 | 1 | train | 138404812158, 138404812137, 138404812184 | 1000062848, 1000062853, 1000062851 | success |

Failures of the probe-budget test among eligible runs: 0 of 13.
Excluded: `land/train-20261124-36` (cancelled) and every lane run with `full=false` (affected selection).

SKIPPED plangraph:LE-PROBE-BUDGET-POST-LANDING-EVIDENCE needs 7 more runs
(the work landed on 2026-11-24; 13 full-tier runs exist so far, 20 are required).
