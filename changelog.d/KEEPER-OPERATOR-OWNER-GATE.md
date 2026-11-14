Operator-owned keeper items now pass a Rust policy gate at creation, import,
definition edits, and kind conversion. The default warn rollout supports an
explicit refuse mode after backlog cleanup. Stored risk/default/deadline rules,
D-2579 standing grants, and authenticated-answer protection prevent ownership
regrowth. Nullable risk and until columns migrate on both keeper backends;
future until dates hide Needs-you rows and snapshot UI badges until due.
