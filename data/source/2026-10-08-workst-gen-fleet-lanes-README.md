# gen-analysis fleet lanes, resumed 2026-10-08

Project key in plangraph: `workst-gen`. Owner: `workst-L1-gen` (the gen-analysis controller session),
orchestrated by `routing` (routing-00; brief routing:docs/plan/briefs/workst-L1-gen-2026-10-08.md, D-667). Data class: PRIVATE (real genetic and laboratory data on disk in
`data/` and `others/`, never in the repository, never sent to any model).

## How work runs (unchanged since 2026-10-03, routing D-514 / OS-119)
- Each lane: a worktree `../gen-analysis-<lane>` on `lane/<lane>` cut from `main`; one writer through
  `AutoOS/tools/autoos-agent.py run --isolate --lean` on a private-safe leg via the workstation gateway;
  the controller judges the result by machine (its own test runs, real exit codes); seats from other
  model families through the workstation runner's folder protocol (`C:\fleet\gen-in\<LANE>\`); a record
  to routing; merge `--no-ff`, changelog, serial full suite on merged `main`, push, read the real
  GitHub Actions run back; cleanup.
- Legs (D-667): private-safe only (OVH, Vertex, Claude); never agy, free or training legs; no paid
  deepseek/muse; OVH only for L3 implementation. Task size PR-sized: at most 3 files plus tests,
  200 lines, 30k context (D-656). Reviews family different from the writer; high tier at least
  3 families (D-657). Every task and sub-task is a plan-graph item in `workst-gen`; CHANGELOG and
  docs with every change (D-649, D-653).

## State on resume
`main` = `origin/main` = `8a02336`; GitHub Actions green on it (2026-10-05) and on `f6f0777` (the
BWERROR merge, 2026-10-03). Between 2026-10-03 and 2026-10-05 the owner's account merged 49 pull
requests written by google-labs-jules[bot] (55 bot commits) and closed 24 unmerged; among the merged
are the five this controller had worked on (73, 75, 76, 77/78/82, 81). None of those 49 merges added a
CHANGELOG entry. 28 bot pull requests (83-110) are open. Of this controller's open
work three items are still real on `main`: the queue gap (QGUARD), the person-id overwrite (PERSONID)
and the questionnaire number parsing (NUMIN).

## Lanes
| id | lane | depends on | done when | tier | estimate |
|---|---|---|---|---|---|
| GEN-QGUARD | Labs uploads are unclaimable by the generic worker from their first statement (`lane/qguard`: tests A/C, the FINDING commit, the approved fix D-532, main merged in) | - | full suite on the lane green; seats from 3 non-writer families (D-657) quote that test B is unchanged; routing ACCEPT; merged and pushed; Actions run green | high | 2 h |
| GEN-PERSONID | a new person never overwrites an existing one; ids are transliterated slugs, numbered on collision, created with a plain INSERT (D-534; brief `.superpowers/fleet/PERSONID-1.brief.md`, gitignored, to be refreshed for the post-PR-32 helpers in `app/api.py`) | - (PR 76 is merged) | the "two Annas 1990" and "same name, no year" tests pass; seats from 3 families; routing ACCEPT; merged; Actions green | high | 3 h |
| GEN-NUMIN | questionnaire numbers refuse nan/inf, non-ASCII digits, scientific notation, "1_000", " +5", with a sentence; covers the admin min/max path | GEN-PERSONID (one writer at a time on api-adjacent code is not needed; ordered after it only to keep one HIGH lane in flight) | tests for each refused form; 1 seat; routing ACCEPT; merged; Actions green | standard | 1.5 h |
| GEN-BWERROR-ACTIONS | read back the GitHub Actions run for the BWERROR merge | - | DONE 2026-10-08: run on `f6f0777` success (and on `8a02336`) | standard | 0.2 h |
| GEN-CHANGELOG-CATCHUP | CHANGELOG entries for the 49 merges of 2026-10-03..05 (grouped by area: bloodwork, provenance, api, questionnaire, tests, logging), docs-only | - | `[Unreleased]` names every merged PR number once; identifier guard green; 1 seat | standard | 1 h |
| GEN-SWAP-OBS-CLOSE | close or reopen omnigraph Task `duckdb-file-move-access-denied-flake-on-windows` | - (dated: on or after 2026-10-10) | every test run since 2026-10-03 printed `atomic-swap retries this session: 0` (Task closed), or the suffixes are recorded (Task reopened) | standard | 0.3 h |
| Q: GEN-PR-OWNER | who triages the 28 open google-labs-jules[bot] pull requests (83-110)? The 49 earlier ones were merged from the owner's account | - | routing names the owner; if it is this controller, one lane per pull request (update branch, CI, read, seat, record) | decision | - |

The operator decisions recorded earlier stay open and are not lanes of this plan: the history rewrite
(`workst-gen:GEN-HISTORY-REWRITE`), the preset and suggestion-rule review, the worker-process lock, two
Stack-Planer rows with the other allele's frequency, and 27 real uploads to file.
