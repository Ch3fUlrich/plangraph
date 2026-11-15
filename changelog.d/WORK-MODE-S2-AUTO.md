# WORK-MODE-S2-AUTO

`pg-spawnd` route JSON and `plangraph_agent_route` now report the resolved
`work_mode` and `work_mode_reason`. Auto selects batch when the existing planner
serves a writer with catalog AA Intelligence at least 50.0; otherwise it selects
piecewise. Exhausted or otherwise unavailable writers do not count. Manual
piecewise and batch settings bypass selection.
