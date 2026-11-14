# Keeper operator ownership gate

`pg-operator-owner-gate` owns the policy in Rust. Keeper create/import,
definition edits, and kind conversion invoke it through the existing bounded
stdin transport. No external backlog or live configuration is changed by this
feature.

Operator ownership means `owner: operator` or `operator: true`. It requires:

- kind `question` or `operator_task`; `rule` and `RULE-*` are ineligible;
- `risk_tag`: `money`, `access`, `secret`, `public`, `irreversible`, `live`, or `taste`;
- a populated `choices.default` and a valid `choices.deadline` date.

Existing choices validation still requires the default to name an option.
`risk_tag`, `operator`, and `until` definition edits require the router role.
SQLite upgrades add nullable `risk_tag` and `until`; PostgreSQL migration 13
adds the same columns. These fields survive show, snapshots, YAML round trips,
and event replay integrity checks.

The standing grant `D-2579` covers these classes, which the gate reports by name:

| Existing `track` class identifier | Descriptive request matched |
| --- | --- |
| `merge_with_go` | merge/merges and the separate word GO |
| `service_restart` | restart/restarts and service/unit/systemd |
| `secret_creation_until_phase1` | create/creation, secret/secrets, and “until phase 1”/“until phase1” |
| `continuity_go_within_budget` | continuity, GO, and “within budget” |

Descriptive matching inspects only the stored title and description, using
whole words; note text cannot supply an authorization or trusted event. This
is a bounded classification list, not a general natural language classifier.
Use a class identifier for requests whose wording differs from the table.

The six risks other than `taste` form the hard set. Their ownership flag,
operator owner, or risk cannot be removed or changed without a current trusted
answer. `answer_question` appends a hash-chained `question_answered` event with
the actual authenticated actor, source, item risk, and epoch. Rust accepts only
`source: answer` from the configured operator principal with router role,
matching the previous risk and epoch, after the latest ownership/risk edit.
Router answers, timeout answers, payloads in item notes, stale epochs, and
events before a later ownership/risk edit do not authorize a change. Reads use
at most the latest 200 item events; older answers require a fresh answer.

`until: YYYY-MM-DD` hides an item from the keeper Needs-you result and the
snapshot-driven Needs-you list and badges until that UTC date. It becomes
visible on the date itself. The snapshot carries `needs_you_hidden`, including
the question refresh path; the underlying item and its dependency edges remain.

The default rollout mode is **warn**. Writes return `operator_owner_gate:`
warnings while preserving existing workflows. After the external backlog sweep,
set `PG_OPERATOR_OWNER_GATE_MODE=refuse` explicitly to refuse violations.
`PG_OPERATOR_OWNER_GATE_BIN` selects an absolute, server-controlled helper path
(default `/usr/local/bin/pg-operator-owner-gate`). A missing or invalid helper
warns in warn mode and fails closed in refuse mode. Parking projection also
needs the helper; an unavailable helper in warn mode leaves rows visible.
`PG_OPERATOR_PRINCIPAL` selects the authenticated operator's principal name
(default `operator`); provision it using the existing principal administration
path with router role. Neither setting is accepted from an item payload.

Build the binary from `rust/` with
`cargo build -p pg-worker-core --bin pg-operator-owner-gate`. The binary accepts
one bounded JSON request on stdin, including an optional `mode: refuse`, and
prints the transport envelope `{"ok":{"refused":false,"warnings":[]}}`.
Production installation and enabling refuse mode are separate deployment steps.

Targeted verification: `pytest tests/test_keeper_operator_owner_gate.py` and
`cargo test -p pg-worker-core --test operator_owner_gate`.
