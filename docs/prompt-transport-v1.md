# Prompt transport v1 — S01

D-5595 / D-5233, with binding D-5632: S01 supplies data and I1 only.
`src/plangraph/config/prompt_transport_v1.json` is packaged by
`pyproject.toml:13-14`; `review_mode.load_prompt_transport_contract()` reads it.
Its single `reasons` object is the authority; fixtures use IDs, not another enum.
Duplicate JSON keys, reason names, harnesses/aliases and unknown reasons refuse.
Clients keep literal exports; drift tests pin harness order and capacity values
to the registry. Launch imports never read the contract resource.
Optional raw JSON must match the entire packaged v1 document, not just reasons.
All launch builders, payload functions and error exports retain their behavior.

## Current-main basis and merge-order gate

Re-pinned to `origin/main` d41bcd1211fa8a15761e0164aeb6b064c40f1c4a.
Read-only GitHub inspection: #789/#791/#753/#796/#794 are MERGED;
#787 is CLOSED/unmerged; #795 remains OPEN. Its spawner changes do not overlap
S01 production files. Historical open-lane lists are not ownership authority.
One writer, sequential slices based on then-current main after the predecessor;
re-read overlaps after intervening merges and coordinate #795 before touching
its files. Do not change PR state or merge this lane (D-5526).
At that base, `src/plangraph/routing_support/clients.py:30,282-293,314-358`
retains seven stdin harnesses/payload exports;
`src/plangraph/routing_support/review_mode.py:66-78` retains launcher/stdin.
`src/plangraph/spawner/runner.py:542-575` still prepares and executes launches;
S01 leaves it untouched (numstat 0/0). Preserve merged #791 behavior at
`src/plangraph/spawner/seat_spawn.py:218-220` and
`src/plangraph/spawner/service.py:299-300`.

## Handoff and evidence

All seven registrations are **contract-only**, not vendor stdin smoke evidence.
S02 consumes metadata version/model bound/title pattern and digest plus fixed
job/model/task fixtures; it adds descriptor validation without changing this table.
The first Rust slice must copy the JSON inside its crate and add byte-drift tests.
Runner I6 assertions belong to S05, persisted I7 evidence to S07, not S01/S04.
Each invariant counts only after its own slice passes; final gates rerun all rows.
I1 checks the resource through `importlib.resources` and pins its package-data glob.
Round-2 evidence: 14 regression failures before fixes; contract and affected
consumer tests pass after fixes. An offline wheel build also resolves the JSON
via `importlib.resources` in an isolated interpreter without the source tree.
S01 done: `PYTHONPATH=src <venv-python> -m pytest -q tests/test_prompt_transport_contract.py`.
The affected selector's unmapped-asset/shared-fixture fallbacks select the whole
suite; this lane's explicit no-whole-suite rule instead uses
`--within` the contract and existing direct client/launcher consumers.
S01 findings: basis/merge order re-pinned above; F10/I1 preserves exports;
reasons include both capacity IDs; vendor evidence remains contract-only.
Later-slice ownership remains unchanged: S04/S05/S07 own I6/I7;
S16/S17 own Windows CreatePipe/handle-list I17; S04/S11 own task/env capacity
precedence; S20 owns concurrent 300 KiB pipe delivery; S09 owns crate-copy drift.
Capacity/provenance/Windows pipes and Rust drift enforcement are later slices,
not claimed complete by I1 or the unchanged launch-path tests.
