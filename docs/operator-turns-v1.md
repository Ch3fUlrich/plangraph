# Operator turns v1

Routing D-6863, slice 1 of 5 (edges D-6867). The data repository pins each verified
operator turn in two git-tracked paths; the router is the only writer, in the same data PR as
the D-row that cites the turn, after its host verifier printed origin human. plangraph code
never writes these paths. This document is version **1**; a format change means a new `v`
value and a new document, never a reinterpretation of `1`. The reader is
`rust/plangraph-cli/src/operator_turns.rs`; every consumer (index builder, CI check, pin verb,
host audit) calls it and adds no second parser.

## Table `router/operator-turns.tsv`

UTF-8, no BOM, LF only, final newline required, no blank or comment lines. Fields are split
on TAB with no quoting or escaping; no field is empty, has outer spaces or contains TAB, CR
or LF. `-` means "none". Line 1 is exactly (TAB-separated):

`v turn_sha256 utc_timestamp kind relay_of session_uuid transcript_line decision lifts withdrawn_by`

A header-only file is valid. Every row has exactly 10 fields; the version is per row because
rows are never rewritten.

1. `v`: the literal `1`; anything else is refused (`version`).
2. `turn_sha256`: 64 lowercase hex, the sha256 of the text file's bytes.
3. `utc_timestamp`: exactly `YYYY-MM-DDTHH:MM:SS.mmmZ`, a valid calendar time (no leap
   second); string order is time order. Turn identity is (`turn_sha256`, `utc_timestamp`):
   short turns such as "go" repeat byte for byte.
4. `kind`: `human` (typed by the operator) or `relay` (a message relaying an operator turn).
5. `relay_of`: `-` for a human row. For a relay, the exact source pin
   `<turn_sha256>@<utc_timestamp>@D-<n>`: a human row of that turn identity and that D-id.
6. `session_uuid`: lowercase 8-4-4-4-12 hex UUID of the transcript holding the text.
7. `transcript_line`: decimal u64 >= 1, no leading zero.
8. `decision`: exactly one canonical `D-<n>` (no leading zero). A row is one pin of one turn
   to one D-id, so rows stay immutable: one turn feeds several D-ids by several rows and one
   D-id cites several turns by several rows.
9. `lifts`: JSON array, `[]` allowed, of objects with exactly the members `item` then `gate`,
   both strings, equal byte for byte to the compact `serde_json` re-serialisation. No
   duplicate pair. `item`: non-empty project, first `:`, non-empty id; no control characters,
   no outer whitespace, everything else (`;`, `(`, `"`, `\`, further `:`) is allowed. `gate`:
   `[a-z][a-z0-9_]{0,31}`; the reader checks the shape only, the consumer rejects unknown names.
10. `withdrawn_by`: `-`, or a canonical `D-<n>` greater than `decision`.

Field faults are `field:<column>` (`version` for `v`), a wrong field count is `columns`, a
wrong header is `header`, CR or a missing final LF is `line_ending`, a non-UTF-8 or BOM table
is `encoding`.

### Cross-row rules

- `turn_conflict`: rows of one turn identity agree on `kind`, `relay_of`, `session_uuid`,
  `transcript_line`.
- `duplicate_pin`: one turn identity and one `decision` occur once.
- `withdrawal_conflict`: withdrawal is decision-wide. Every row of a D-id, later appends
  included, carries the same `withdrawn_by`, so a sibling pin never keeps a withdrawn
  decision's lift live.
- `relay_source_missing`: the pin named by `relay_of` must exist, be `human` and not be later
  than the relay. A relay never rebinds to another pin of the same text.
- `relay_scope`: every lift of a relay is a lift of that exact source pin; a relay may
  narrow, never widen.
- A lift is live when its row has `withdrawn_by == -` and, for a relay, its source pin has
  `withdrawn_by == -` too. `live_lifts(turns, decision)` is that rule; a sibling pin of the
  source turn, or a later pin of the same text, never stands in for the named source.

### D-row turn reference

The D-row of a decision carries a structured reference for each turn pinned to it:
`<turn_sha256>@<utc_timestamp>` (`PinnedTurn::turn_ref`). The index builder and the CI check
require this set to equal the `turn_ref` set of the rows with that `decision`; a pin the
D-row never cites, or a citation without a pin, assigns no operator origin.

## Text `router/operator-turns/<turn_sha256>.txt`

The verbatim turn bytes, nothing added or wrapped. A non-empty regular file (no symlink)
whose sha256 equals its name. Rows may share a file. The directory holds only files named by
some row (`orphan_text`). Faults: `text_missing`, `text_not_regular`, `text_hash`. The reader
never decodes or prints the bytes; `detail` holds only column names, hashes, D-ids and item
keys. `chars` and `text_path` of D-6863 are dropped: both derive from the file name, and a
stored path is a second truth.

## State and ownership

- Immutable: rows are append-only; fields 1-9 never change; text files are never edited or
  deleted, withdrawn ones included. The one allowed mutation is `withdrawn_by` from `-` to a
  D-id, once per D-id, on every row of it.
- The reader validates one tree state. The history rules above are enforced by the later CI
  slice.
- Authority is the git tree, not a working directory. `operator_turns::validate(tsv, texts)`
  takes the table and the complete inventory of `Entry { name, regular, bytes }`: the index
  builder and CI feed it from one pinned git tree (entry mode regular, raw blob bytes). `load`
  builds the same inventory from a directory for fixtures and pre-commit checks and refuses a
  symlinked `router/`, `router/operator-turns/` or table (`path_not_real`, `tsv_not_regular`).
  Text rules run once the table parses. All problems are returned, ordered by line; any
  problem refuses the whole file. `Problem` displays as
  `router/operator-turns.tsv:<line>: <code>: <detail>`, line 0 meaning file level.

## Fixtures

`tests/fixtures/operator_turns/<case>/router/...`, synthetic texts:

- `valid`: a release (D-101, two lifts, one with `;`, `(` and `:`), its relay (D-102), a
  withdrawn release (D-103, withdrawn by D-105) with a relay of it (D-104, no live lift), a
  second pin of the first turn (D-106) and a later turn of the same text `go` pinned twice
  (D-107, D-108).
- `relay-scope-mismatch`: a relay that lifts `demo:C` its source pin does not hold.
- The withdrawn release is rows 4 and 5 of `valid`; the backfill and sibling-pin cases are
  built from it in the tests.

Checkouts must keep the hashed bytes (no line-ending conversion); a changed byte shows as
`text_hash`.

## Open points (routing)

Lifts live in the table and `chars` and `text_path` are dropped against the wording of
D-6863; the router records a D-row amending D-6863 before the pin-verb slice. Retroactive
rows take the millisecond time from the transcript, never a guessed `.000`.
