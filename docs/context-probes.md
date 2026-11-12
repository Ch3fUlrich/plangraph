# Context probes: offline slice 1

`rust/pg-probe` is the offline CONTEXT-PROBES library (PH-CTX-TRUTH T1).
Check: `cd rust && cargo test -p pg-probe`. No sessions, processes or network calls.

`load(BufRead)` preserves JSONL order: `id`, `kind` (`rule`/`knowledge`), `question`,
nonblank string `gold` (rule number/D-id/graph fact), `expected_path`, `planner_proposed`.
Paths: `{"tool":"context"}` or `{"file_class":"repository_source"}`. Blank lines fail.
Named `LoadError`s carry one-based line numbers: `DuplicateId`, `MissingGold`
(also null/blank), `UnknownKind`, `InvalidJson`, `Io`. The sample has three rule and
two knowledge probes, planner-proposed, not operator-approved.

`RunRecord`: `final_answer`, `cited_ids`, `tool_calls` (tool, optional file class).
Caller-supplied file classes are trusted; the scorer cannot verify tool execution.
`score` returns independent binary numbers, so wrong-path answers lose only path credit:
- `answer_accuracy`: lowercase alphanumeric gold tokens occur consecutively in the
  answer, ignoring punctuation/spacing, not matching id prefixes. Lexical matching
  only: no semantic adjudication or contradiction detection.
- `source_currency`: nonempty citations, **every** id `in_force`; unknown/stale ids fail.
- `retrieval_path`: any call exactly matches the expected tool or file class.
Scores carry `probe_id`, reasons for every dimension, and the entire `RunConfig`:
`leg`, `fresh_session`, `tool_set`, `repo_sha`, `no_memory_dir` (recorded, not enforced).
Scores/JSON use no clocks, randomness or map iteration and are deterministic.

Later slices own the full 40+10 probes, pinned session launcher, baseline keeper
record **before cleanup/skill-fix merges**, daily remote timer (max four concurrent),
operator spot-check and rescoring. This slice does not satisfy that live baseline gate.
