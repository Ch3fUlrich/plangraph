Keeper priorities now use integers 0–10 (10 most important, 0 optional).
Define/import, priority, edit and batch writes share validation and refuse
out-of-range values with `validation`; CLI help documents the scale.
Rust regression tests cover refusals without item/event mutations, all valid
priorities, and the unchanged oversight stale-gate threshold of 9.
An explicit `null` priority in define/import is now refused as `validation`
(it used to be coerced to 0); omit the field for the default 0.
