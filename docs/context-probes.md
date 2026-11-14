# Context probes: offline slice 1

`rust/pg-probe` is the offline CONTEXT-PROBES library (PH-CTX-TRUTH T1).
The shared `rust/Cargo.toml` keeps all workspace members, including `pg-probe` and `pg-spawnd`, in alphabetical order.
Check: `cd rust && cargo test -p pg-probe`. No sessions, processes or network calls.
Shared `rust/Cargo.toml` and `rust/Cargo.lock` changes follow the Rust workspace
guidance in [CONTRIBUTING.md](../CONTRIBUTING.md); this guide covers `pg-probe` only.

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

## Proposed daily inventory and pinned runner

`fixtures/daily-probes.jsonl` adds 40 rule and 10 knowledge probes. They are
planner-proposed, covering repository policy, cross-repository launch ownership,
transport aliases and component owners. Rule gold IDs are grounded in
`CONTRIBUTING.md` and `docs/SPAWNER.md`; knowledge gold comes from workspace
manifests, the transport registry, `.docs-map` and the documented external router
path. Repeated decisions exercise distinct policy questions. They have not been
operator spot-checked or validated against the private live rule store.

Build `cd rust && cargo build --release -p pg-probe --bin pg-probe`.
`pg-probe run PINNED.json PROBES.jsonl SOURCES.json OUTPUT.json` runs a baseline.
`daily` uses the same inputs and an output directory, creating a unique run file.
`rescore RUN.json PROBES.jsonl SOURCES.json OUTPUT.json` reuses recorded raw
responses with corrected gold/current-source statuses without launching models.
Outputs are synced and published without overwriting an existing run; failures
produce no successful run artifact. Originals remain available for later rescoring.

Pinned configuration has this shape (replace the adapter hash and repository SHA
with the reviewed adapter's actual digest and the selected repository revision):

```json
{"config":{"leg":"selected-leg","fresh_session":true,"tool_set":["context","read"],"repo_sha":"0123456789abcdef0123456789abcdef01234567","no_memory_dir":true},"adapter":"/opt/plangraph/bin/context-probe-leg","adapter_sha256":"REPLACE_WITH_SHA256","args":[],"timeout_seconds":300,"concurrency":4}
```

The runner checks the executable hash and configuration before launch, limits
concurrency to 1–4, and uses a separate private temporary session directory for
every probe. HOME/XDG/TMP point there; inherited environment (including all
`GIT_*` and memory/config overrides) is cleared except PATH and Windows loader
roots. The shared Rust watchdog bounds adapter runtime and output and terminates
its process group/job. Only the question, ID and pinned config are sent, never
gold or in-force answers. The adapter reads `PG_PROBE_REQUEST_FILE`, launches the
selected fresh leg with the exact tools/repository SHA and no persistent memory,
and writes a `RunRecord` JSON object to `PG_PROBE_RESPONSE_FILE`. The response
must be at most 1 MiB and must not be a symlink.

The adapter is an explicit reviewed integration boundary: the generic runner
cannot prove that it honored the requested model/tools/revision or actually
retrieved cited sources. A production adapter is not supplied in this slice.
Its implementation and pin must be established before live baseline collection.
`SOURCES.json` maps citation IDs to `in_force`, `superseded` or `history`; collect
it from current truth, never fabricate it from the fixture. Each output includes
raw responses, full config/probes/source snapshot and SHA256 input identities.

Before any dependent cleanup/skill-fix merge, the operator must run the real
baseline and persist its artifact in keeper using the deployment's artifact
interface. That keeper interface and a live baseline were unavailable here;
a local file or fake-adapter test is not keeper baseline evidence. The operator
may spot-check ten probes later, correct the inventory and rescore old runs.

On the designated probe host, after establishing the adapter and baseline:

```sh
pg-probe run "$HOME/.config/plangraph/context-probes/pinned.json" rust/pg-probe/fixtures/daily-probes.jsonl "$HOME/.config/plangraph/context-probes/sources.json" "$HOME/.local/state/plangraph/context-probes/baseline.json"
install -Dm644 deploy/systemd/pg-context-probes.service "$HOME/.config/systemd/user/pg-context-probes.service"
install -Dm644 deploy/systemd/pg-context-probes.timer "$HOME/.config/systemd/user/pg-context-probes.timer"
install -Dm644 rust/pg-probe/fixtures/daily-probes.jsonl "$HOME/.config/plangraph/context-probes/probes.jsonl"
systemctl --user daemon-reload
systemctl --user enable --now pg-context-probes.timer
```

Install `rust/target/release/pg-probe` as `$HOME/.local/bin/pg-probe` first. No
units were installed or live agents launched by the hermetic runner tests.
