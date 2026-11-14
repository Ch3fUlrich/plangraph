# Offline plan lint

`pg-plan-lint --snapshot snapshot.json` reports each item's 0–10 quality score,
finding codes and fixes. Add `--json` for structured output. Exit codes are 0
(clean), 1 (findings), and 2 (invalid arguments/input). Build with
`cargo build -p pg-worker-core --bin pg-plan-lint` from `rust/`.

The JSON input is an item array or `{ "items": [...], "running": [...] }`.
Items use `id` (or `key`), `title`, `owner`, `track`, `description`, `est_h`
(or `estimate_h`), `depends_on`, and optional `status`. Card fields may appear
under `card` or on the item: `criteria`, `check_cmd`, `write_set`, `read_set`,
and `heldout_check`. Criteria rows have `id`, `text`, `check_ref`, and `class`
(`code`, `judged`, or `attested`). Check commands accept a string or ordered
string array. The running array and peers with status `claimed` or `running`
reserve their write paths. Dependency metadata comes from the items array.

The reusable Rust API is `pg_worker_core::plan_lint::{lint_item,lint_snapshot}`.
`lint_item(item, peers)` checks one item; `lint_snapshot(snapshot)` also reads
explicit running reservations. Both return serializable reports containing
`key`, `score`, `codes`, `findings` and one combined `message`. Each finding
includes its code and a concrete fix. Scores subtract one point per distinct
finding code from ten, bounded at zero. These are deterministic lint scores,
not a statistical calibration against historical production plans.

Checks cover titles/owners/tracks, Why/How references, 3–8 criteria with checks
and a code row, command structure, repository-relative bounded write sets,
running overlaps, four-hour estimates, data/decision dependencies, and forbidden
follow-up suffixes. Criteria also reject vague words, process text, missing
clause checks, restated external list sizes and unenumerated all/every claims.
Duplicate criteria use Unicode SequenceMatcher similarity with difflib's default
autojunk behavior and a 0.85 threshold within one track. Text is lowercased and
rows are joined with newlines before comparison. Glob reservations use a
conservative overlap check, so uncertain overlapping prefixes may be refused.

This library is offline. Command validation checks structure and balanced quotes;
it neither runs commands nor verifies executable availability, file existence,
check results, keeper state or policy cutoffs. Those belong to later gates.
Named check references are allowed; `check[n]` must reference a command index.
A semicolon-separated criterion needs a comma-separated reference per clause.
The synthetic tests include twenty hand-scored cards and boundary/context cases:
`python3 -m pytest -q tests/test_plan_lint.py` builds and invokes the real Rust CLI.
