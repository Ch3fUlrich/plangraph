# Python to Rust migration plan (D-5912)

New code is Rust with Rust tests; no new Python or shell file; an allowlist line
needs an operator D-id. Existing Python fixes remain allowed until each port.

The language gate now runs in `rust/pg-policy`; `tools/check_new_code_lang.py`
is its compatibility launcher. Both policy and allowlist come from the selected
Git revisions. Newly added allowlist entries require a D-id marked OPERATOR in
the merge-base snapshot `tools/new_code_decisions_index.txt`. An index addition
cannot authorize the same PR; merge that authority separately. D-5840 and the
superseded D-5830 never authorize additions. Removals need no decision. The
snapshot records D-5912 from `router/docs/decisions-log.md`; maintain it from
operator decisions, with the normal independent review and merge gates.

First remaining ports, preserving the three accepted in-flight PRs from D-5912:

- #881: `src/plangraph/fleet_sync/head_audit.py`.
- #896: `src/plangraph/retired_models.py` and `src/plangraph/model_admission.py`.
- base-fresh-gate: `src/plangraph/spawner/base_fresh.py`.

Each port needs Rust behavioral tests, parity against existing callers, then
caller migration and removal of its obsolete allowlist entry. No future-port
promise authorizes another Python file. The spawner pack and planner brief
include the language line above.
