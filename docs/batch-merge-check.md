# Batch merge admission (rule 3.39, D-5938 / D-6019)

`plangraph batch-merge-check FACTS.json` is a side-effect-free Rust admission
check for router callers. Exit 0 prints the admitted head and batch part numbers;
exit 1 prints a refusal such as `batch_required`. It runs before home/environment
setup, from a repository with the candidate and part commit objects available.
It never merges, pushes, reviews, collects evidence or authorizes a GO.

This is the first slice of LE-BATCH-MERGE-ENFORCED. The external
`router/bin/merge-part`, `go-row-gate`, plan-order launcher and keeper display
are absent from this checkout. They are **not yet wired** to this check.
Their integration and end-to-end merge refusal test remain open.

## Evidence contract for the external adapter

The adapter supplies a complete open part-PR inventory in `ready`, with exact
heads, head-bound review results (`ACCEPT`, `REQUEST_CHANGES`, `PENDING`),
`fast_green`, and RFC3339 `ready_at`. For a non-ready PR use the evidence
observation time; for a ready PR use the time this exact head first had both
ACCEPT and green fast gates. Unknown evidence must stop the adapter, never
become an empty inventory. Exclude the batch PR from this inventory.

The compact `go_row` representation is exactly `GO <number> <full-head>` with
one optional exception token. The adapter must derive it from the recorded GO
for that PR and head, rather than inventing permission. A single PR requires:

- `EXCEPTION:main-red-fix`: the adapter verifies the PR is the main-red fix (3.19).
- `EXCEPTION:only-one-ready-pr`: the check independently requires exactly one ready PR.
- `EXCEPTION:operator-order`: the adapter verifies the operator ordered this PR.

`batch` is null for a single PR. For a batch it contains its `pr`, `head`,
`pushed_at` and `parts` (the frozen ready records). Persist that manifest at the
first successful branch push, before starting CI. Repeating a push/CI lookup
must not move the cutoff. A changed batch head needs a fresh manifest and CI.
The check verifies each part was ready at push and is an ancestor of the batch
head. A currently ready PR absent from the manifest blocks only if its
`ready_at <= pushed_at`; later arrivals belong to the next batch.
All timestamps and manifests come from trusted evidence storage, not PR bodies.
The `parts=...` result gives the adapter the part numbers for keeper display.

Example single-PR facts (replace the example SHA with a full commit SHA):

```json
{"pr":1,"head":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
 "go_row":"GO 1 aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa EXCEPTION:only-one-ready-pr",
 "ready":[{"pr":1,"head":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
           "review":"ACCEPT","fast_green":true,"ready_at":"2026-11-15T05:00:00Z"}],
 "batch":null}
```

## Plan-order runbook

Collect all ACCEPT + fast-green heads, check the existing planner's caps and
risk grouping, and pass the complete eligible group to the existing builder
**in one call** (no new batch builder):

```console
python3 tools/merge_batch.py build --name NAME --prs 899,904 --plan plangraph:ITEM
```

Do not use `--force-set` or skip conflicts. Resolve a conflict and `continue`
the same batch. Record a keeper-bound spawner review for each part's Plan-Item
with `review --name NAME --pr NUMBER --run-id RUN_ID`. The command reads
`spawner_records_dir` from the local keeper configuration. Use
`--file PATH` to record a supplementary review. Run `push --name NAME` and
persist the freeze manifest.
Run one full CI at the pushed batch head, then record it with `push --name NAME
--ci-run RUN_ID`. Obtain one Hydrogen remerge-diff read at that exact head.
`go-req --name NAME` prints one GO-REQ with per-part evidence and PLAN block;
record its GO before `land --name NAME --batch-pr NUMBER --approval PATH`.
The Rust check supplements the existing review, main-green, CI and GO gates;
it does not replace them. Wiring it into both external entrypoints is required
before claiming rule 3.39 is enforced in production.
