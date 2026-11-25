# Operator turns v1

Routing D-6863, amended by D-6872 (lifts in the table, `chars` and `text_path` dropped);
slice 1 of 5 (edges D-6867). The data repository pins each verified
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
5. `relay_of`: `-` for a human row. For a relay, one or more exact source pins
   `<turn_sha256>@<utc_timestamp>@D-<n>`, comma-joined, all of ONE human turn (same hash and time), strictly ascending by D-id, no
   duplicate: each is a human row of that turn identity and that D-id. A relay whose lifts
   come from several pins of one human turn names each of them.
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

- `turn_conflict`: rows of one turn identity agree on `kind`, the relayed turn (hash and time of
  the pins in `relay_of`; the pin lists may differ per row), `session_uuid`,
  `transcript_line`.
- `duplicate_pin`: one turn identity and one `decision` occur once.
- `withdrawal_conflict`: withdrawal is decision-wide. Every row of a D-id, later appends
  included, carries the same `withdrawn_by`, so a sibling pin never keeps a withdrawn
  decision's lift live.
- `relay_source_missing`: every pin named by `relay_of` must exist, be `human` and not be
  later than the relay. A relay never rebinds to another pin of the same text.
- `relay_scope`: every lift of a relay is a lift of at least one named source pin; a relay
  may narrow, never widen.
- A lift is live when its row has `withdrawn_by == -` and, for a relay, some named source
  pin that holds the same lift has `withdrawn_by == -` too. `live_lifts(turns, decision)` is
  that rule; a sibling pin of the source turn, or a later pin of the same text, that the
  relay does not name never stands in for a source.

### D-row turn reference

The D-row of a decision cites its pinned turns with one statement inside its text cell,
matched as a whole token (never a pattern over prose): the token
`operator-turns=<ref>[,<ref>...]`, delimited by whitespace, where `<ref>` is
`<turn_sha256>@<utc_timestamp>` (`PinnedTurn::turn_ref`), refs distinct and strictly
ascending as text. A D-row without the token cites no turn; two tokens in one cell refuse the
cell. `operator_turns::citation(turns, decision)` writes the token and
`operator_turns::cited_refs(cell)` reads it. The index builder and the CI check require the
cited set to equal the `turn_ref` set of the rows with that `decision`; a pin the D-row never
cites, or a citation without a pin, assigns no operator origin. A D-row citing two turns
(the D-6799 shape) reads:

`| D-201 | plangraph | ... operator-turns=0523...28f7@2026-01-05T10:30:00.000Z,a752...c384@2026-01-05T10:00:00.000Z ... |`

with two rows of the table, one per turn, both `decision` `D-201`.

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

A checkout with `core.autocrlf` converts the hashed bytes and the table; no fixture holds a
CR, so the Rust tests materialize the fixtures into a temporary directory with CRLF restored
to LF (`checkout_with_crlf_is_restored_when_materialized`). A consumer that reads the
checked-in files directly sees a changed byte as `text_hash` or `line_ending`. A `-text`
attribute for the fixtures is a gate path and a separate slice.

## Decisions

D-6872 accepted both amendments of D-6863: lifts live in the table row beside the turn they
release, and `chars` and `text_path` are dropped. Everything else of D-6863 stands.
Retroactive rows take the millisecond time from the transcript, never a guessed `.000`.
