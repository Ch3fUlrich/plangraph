# Prompt transport v1 — S01

D-5595 / D-5233, with binding D-5632: S01 supplies data and I1 only.
`src/plangraph/config/prompt_transport_v1.json` is packaged by
`pyproject.toml:13-14`; `review_mode.load_prompt_transport_contract()` reads it.
Its single `reasons` object is the authority; fixtures use IDs, not another enum.
Duplicate JSON keys, reason names, harnesses/aliases and unknown reasons refuse.
Clients keep literal exports; drift tests pin harness order and the two Windows
argv limits, not POSIX or Windows environment capacities (later-slice data).
Launch imports never read the resource; subprocess tests trap opener/loader calls.
The fresh interpreter explicitly uses this checkout's `src/`, independent of an
installed package or inherited `PYTHONPATH`; pytest's `sys.path` is not inherited.
Both missing-resource and parse failures propagate only on the first loader use.
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
