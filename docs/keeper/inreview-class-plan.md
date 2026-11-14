# D-5572(4) historical review classification plan

Status: **PROPOSED, not routed or independently accepted**. The historical
refusal exports and matching 210-key snapshot are absent from this checkout and
its primary checkout. No key lists, keeper attachments, routing decision or
external review result have been invented. The table below is the proposed
policy catalog; the Rust tool produces its complete per-class key lists only
from the original exports.

| Class | Path to a verdict or out of review | Owner | Rules |
|---|---|---|---|
| no record | One unpinned route seat per missing family on the merged head; run through the spawner to obtain a record | Each project's L1; routing close-list owner | D-5092, D-5553, D-3551 |
| pinned seat | Replace with an unpinned route seat per missing family on the merged head, with a complete spawner record | Each project's L1 | D-5092, D-5553, D-3551 |
| served_model unwitnessed | Obtain a witnessed, unpinned route seat per missing family on the merged head through the spawner | Each project's L1 | D-5092, D-5553, D-3551 |
| unknown writer | Resolve Writer trailers and lane-run records; take the union of candidate writer families. Unresolved keys need seats outside every candidate family or an explicit operator waiver list | Each project's L1 supplies witnesses/seats; routing owns waiver list | D-5572(4), D-5092, D-5553, D-3551 |
| no commit evidence | Find merged Plan-Item commit or PR and attach commit+ci. Nothing merged means routed release to open or guarded cancel; never post a verdict | Each project's L1 finds proof; routing rules release/cancel | D-5572(4), D-3551, KEEPER-HYGIENE |
| head mismatch | Propose merge-only delta acceptance with tree-diff proof through close-merged; otherwise a delta seat. Name ambiguous verified commits per key in the router close list | Each project's L1 supplies tree/delta proof; routing rules ambiguity and acceptance | D-5572(4) proposal requires ruling; D-3551, INREVIEW-CLOSE-ROUTER-PATH |
| item key mismatch | Exact key and PR in a router close-list row; use the existing guarded close path. No new seat solely for identity repair | Routing close-list owner; project's L1 supplies evidence | D-5572(4), D-3551, INREVIEW-CLOSE-ROUTER-PATH |
| PR number mismatch | Exact key and PR in a router close-list row; resolve through the guarded close path. No new seat solely for the label mismatch | Routing close-list owner; project's L1 supplies evidence | D-5572(4), D-3551, INREVIEW-CLOSE-ROUTER-PATH |

All verdicts still pass through `tools/seat_verdict.py` or `pg close-merged`.
This proposal does not weaken the poster's exact-head rule, recorded-seat
requirements, writer independence, or GO-head checks. A routing ruling must
confirm or replace the proposed paths; a table alone grants no new authority.

Generate the actual table and keeper definition proposals with:

```text
pg-ctx backfill-plan snapshot.json report-primary.json report-secondary.json
pg-ctx backfill-plan snapshot.json report-primary.json report-secondary.json --markdown
```

The default cohort is exactly 210 in_review keys. `--expected COUNT` exists for
fixtures and explicitly revised cohorts. Inputs use the existing seat backfill
JSON format: an array of `{key,status,reasons,...}` rows. Each source must have
unique keys; sources may overlap or contain different subsets, but their union
must cover the entire cohort. Unexpected keys, eligible rows, unknown refusal
classes, absent reasons or count mismatches fail instead of silently omitting
work. No file or keeper state is written by this command.

Primary precedence is key mismatch, PR mismatch, no commit evidence, unknown
writer, head mismatch, pinned seat, unwitnessed model, no record. A worker with
no records cannot override more specific evidence from another worker. Each key
occurs in exactly one primary list; additional classes remain secondary lists.
Ambiguous verified commits are included in head mismatch. The output includes
all eight rows even when a primary list is empty, the remediation path, rule
citations, class owners and a per-key project-L1 owner map.

The JSON companion includes follow-up definition proposals, grouped by project
and primary class, with dependencies on
`plangraph:INREVIEW-BACKFILL-CLASS-PLAN` as the routing/review gate. Owners are
logical L1 roles until the orchestrator substitutes registered principals.
The five supported projects are plangraph, server, autoos, project-alpha and
project-beta; the last two are neutral project labels. Map private project IDs
to these labels consistently in the snapshot and refusal reports before running
the tool. Each project's L1 owns its keys' seats. The output never
claims those leaves or gate edges have already been written to the keeper.

Orchestrator handoff: provide the original exports and snapshot; generate and
attach the table/JSON; obtain the ruling D-id and another model family's plan
review; require ACCEPT; define the follow-up leaves with the proposed gate
edges; then route their execution. The live-write prohibition in this package
leaves those steps outside this worktree's execution.
