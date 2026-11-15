# WORK-MODE-S2-AUTO

`pg-spawnd` route JSON and `plangraph_agent_route` now report the resolved
`work_mode` and `work_mode_reason`. Auto selects batch when the existing planner
serves a writer with catalog AA Intelligence at least 50.0; otherwise it selects
piecewise. Exhausted or otherwise unavailable writers do not count. Manual
piecewise and batch settings bypass selection.

The writer snapshot resolves each served leg to its catalog score through the
leg model, harness-prefixed model, upstream aliases and an explicit leg map, so
the real Codex and Argon writer legs reach the threshold. A served leg with no
catalog row is reported as unscored in `work_mode_reason`.
