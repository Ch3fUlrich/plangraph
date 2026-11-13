# Prompt transport v1 — S01

D-5595 / D-5233, with binding D-5632: S01 supplies data and I1 only.
`src/plangraph/config/prompt_transport_v1.json` is packaged by
`pyproject.toml:13-14`; `review_mode.load_prompt_transport_contract()` reads it.
Its single `reasons` object is the authority; fixtures use IDs, not another enum.
Duplicate JSON keys, reason names, harnesses/aliases and unknown reasons refuse.
Clients keep literal exports; drift tests pin harness order and the two Windows
argv limits, not POSIX or Windows environment capacities (later-slice data).
Launch imports never read the resource; subprocess tests trap opener/loader calls.
Optional raw JSON validates reasons and harnesses/aliases before full-document
equality rejects changes to capacity, flags, metadata, status, grammar or modes.
The parsed authority is lazy-cached; callers get deep copies and tests can reset it.
Claude's `forbidden_flags: ["--bg"]` is pinned data, not launch enforcement in S01.
All launch builders, payload functions and error exports retain their behavior.

## Basis and merge-order gate
Basis: `origin/main` d41bcd1211fa8a15761e0164aeb6b064c40f1c4a; re-read overlaps
after intervening merges. S01 leaves clients/runner/seat_spawn/service untouched,
preserving #791 and #795 behavior. One writer, sequential slices on current main
after predecessors; never merge this lane or change PR state (D-5526).

## Handoff and evidence
All seven registrations are **contract-only**, not vendor stdin smoke evidence.
S02 consumes metadata version/model bound/title pattern and digest plus fixed
job/model/task fixtures; it adds descriptor validation without changing this table.
The first Rust slice must copy the JSON inside its crate and add byte-drift tests.
Runner I6 assertions belong to S05, persisted I7 evidence to S07, not S01/S04.
I1 checks the source-tree resource through `importlib.resources` and pins its
package-data glob using a repository-relative path; installed wheels are not tested.
The sandbox clears a polluted environment except platform process variables,
then installs isolated home/config paths and blocks live probes/network/processes.
S01 done: `PYTHONPATH=src <venv-python> -m pytest -q tests/test_prompt_transport_contract.py`.
The affected selector's shared-fixture fallback uses `--within` the contract and
direct client/launcher consumers to honor the no-whole-suite rule.
Later-slice ownership remains unchanged: S04/S05/S07 own I6/I7;
S16/S17 own Windows CreatePipe/handle-list I17; S04/S11 own task/env capacity
precedence; S20 owns concurrent 300 KiB pipe delivery; S09 owns crate-copy drift.
Capacity/provenance, grammar/forbidden-flag admission, Windows pipes and Rust drift
enforcement are later slices, not claimed complete by I1.
