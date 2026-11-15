- D-5874 / D-6054: Record-derived review verdicts and backfill accept an override
  whose reason starts with `D-<n> operator` only when that exact decision is indexed
  as an operator decision. Explicit `pin`, `pinned`, and `spawn_pin` flags still
  refuse; served-model witnessing and completion HEAD binding remain required.
- The authority is `router/decisions-index.tsv` in the data repository named by
  `PG_DATA_DIR`. Unset or empty `PG_DATA_DIR` refuses with `PG_DATA_DIR_UNSET`;
  no sibling path is guessed. TSV columns are decision id then source
  (`OPERATOR`, case insensitive); extra columns and a header are permitted.
  Missing, unreadable, malformed or duplicate decision rows refuse the exemption.
  Rust integration tests cover verdict posting, backfill discovery and refusals.
