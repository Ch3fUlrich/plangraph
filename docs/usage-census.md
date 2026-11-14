# Claude usage census

Build/install `pg-usage-census` from `pg-worker-core`, or set the absolute
`PG_USAGE_CENSUS_BIN` path. Run `python tools/claude_usage.py --census --since 7d --json`.
`--projects`, `--until`, `--repo`, and offline `--pr-cache` (gh JSON containing
number/mergedAt) select inputs. Live collection uses read-only `gh pr list` and
refuses a saturated result limit. Transcript content and credential values are
never included in the report. Symlinked directories/files are skipped.

Weights are input/output/cache-write = 1 and cache-read = 0.1. Repeated assistant
message IDs deduplicate within each transcript file; child identity stays separate.
Session custom-title/agent-name supplies role. Explicit `trigger` metadata supplies
msg-in/coordination-tool-loop/beat/work; explicit `spawn_kind` supplies fork/fresh.
Missing provenance remains unknown and must be instrumented before drawing claims.
Per-PR values are window rates, rather than attribution to particular changes.
The last three complete UTC days below 58 merged PRs each emit `revert-cut`;
the operator/router must revert the cut. This read-only tool never changes code
or keeper state. Save the first real report locally and attach through routing.
