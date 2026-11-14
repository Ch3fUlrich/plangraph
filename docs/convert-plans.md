# Plan conversion

`python3 -m plangraph.keeper.cli plan convert PLAN_DIR --cards SNAPSHOT.json --output OUTPUT_DIR`
converts typed plan data into one atomic keeper batch. It runs entirely offline.
Use `--project PROJECT` for a project other than the default. The output directory
must be fresh; validation failures leave it absent.

The cards input is a complete local keeper JSON snapshot, including `items` and
`head_seq` (the legacy spelling `seq` is accepted). `--base-seq N` defaults to that
snapshot sequence. A snapshot older than the requested base is refused. A newer
snapshot must include `item_seqs`, mapping **every** card key to its last event
sequence. The keeper's existing optimistic precondition checks those event marks;
missing marks cannot establish that an item stayed unchanged. Conversion proves
validity against these supplied inputs. Applying later still requires the keeper's
live `base_seq` check.

By default the converter reads `items.yaml` and `changes.yaml`. Repeat `--data FILE`
to choose another ordered set, such as followup data. `--batch NAME` and
`--principal NAME` override the batch metadata. New items use `est_h` as
`estimate_h`, qualify dependency keys, inherit their parent's track when absent,
and attach through an explicit parent edit. New parents are updated within the
single create operation. Criteria and title rewrites append provenance to notes.

Supported changes are `rewrite_dc`, `reowner`, `edit`/`edits`, `reedge`, `hold`,
`unhold`, `cancel`, `merge`, and `reprio`. Followup edits accept `criteria`, exact
`replace: [{old: ..., new: ...}]` replacements, dependency add/remove mappings,
and priority. Structured `bulk.edges` rows accept `[source, target, drop, null]`
or `[source, target, redirect, replacement]`. Merge rows cancel the source and
append provenance to the survivor; dependency moves must be explicit in reedge
or bulk edge rows. No policy is inferred from prose.

The manifest reports `close`, `decide`, and prose-only bulk rows as
`not_converted`: those need their evidence or planning workflow. Other metadata
(such as audit references, counts, and conflict explanations) supplies context.
Explicit edits refused by an item's state stop conversion; they are never silently
omitted. Missing cards, oversize batches or fields, invalid credentials, dependency
caps, seat clauses below high tier, and invalid final graphs stop with a named code.
The existing seat validator is called before edit auto-promotion; cancelled-edge
warnings from keeper replay are treated as refusals.

`batch.yaml` contains one create operation for all new items, one edit per touched
item, then cancels, then priorities. Each group is sorted by qualified item key.
`manifest.json` hashes the actual bytes of every data file and cards input plus
the ordered operations. Canonical operation JSON uses sorted keys, UTF-8, no extra
whitespace, and no ASCII escaping. The batch pins the manifest's exact bytes.
Input files changing between decode and hashing are refused. Both outputs are
published together by renaming a prepared directory after validation succeeds.

The Rust implementation is `rust/worker-core/src/plan_convert.rs`, with input
shape checks and reporting in `plan_convert_data.rs`, and JSON transport in
`src/bin/pg-plan-convert.rs`. Existing Python `planner/service.py` only decodes
YAML, invokes Rust, seeds disposable SQLite, calls the keeper's validators and
`Store.apply_ops(..., dry_run=True)`, and writes the output. It never connects to
a running keeper. `PG_PLAN_CONVERT_BIN` may select an already-built binary;
otherwise Cargo runs offline against the checked-out workspace.

Run the focused tests with `python3 -m pytest -q tests/test_plan_convert.py`.
Set `PG_PLAN_CONVERT_FIXTURE_DIR` to a private plan directory for the historical
main and followup fixture checks. Their outputs stay under that data repository's `.w3-artifacts`.
Without `PG_PLAN_CONVERT_CARDS`, this check replays the available historical YAML,
labels that baseline reconstructed, and checks that missing keeper cards or a
replacement without its exact old criterion are refused. It cannot prove a successful historical apply: the repository does not
contain the complete pre-plan keeper snapshot. Supply that snapshot through
`PG_PLAN_CONVERT_CARDS` (main) or `PG_PLAN_CONVERT_FOLLOWUP_CARDS` (followup) to
require successful conversion at the respective historical base.
No private fixture content is stored in public tests or documentation.
