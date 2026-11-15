- D-5874 / D-6054: Record-derived review verdicts and backfill accept an override
  whose reason starts with `D-<n> operator` only when that exact decision is indexed
  as an operator decision. Explicit `pin`, `pinned`, and `spawn_pin` flags still
  refuse; served-model witnessing and completion HEAD binding remain required.
- The authority is `router/decisions-index.tsv` under `PG_DATA_DIR` (default:
  the sibling `plangraph-data` checkout). TSV columns are decision id then source
  (`OPERATOR`, case insensitive); extra columns and a header are permitted.
  Missing, unreadable, malformed or duplicate decision rows refuse the exemption.
  Rust integration tests cover verdict posting, backfill discovery and refusals.
