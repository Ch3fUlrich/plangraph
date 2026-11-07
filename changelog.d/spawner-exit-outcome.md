### SPAWNER-EXIT-OUTCOME

OpenCode launches request JSON completion framing. A stop-completed text answer
followed by exit 1 with `Error: terminated` now records status and event
`terminated_after_answer`, preserving the exit code instead of calling it a model
failure or silently promoting a verdict to success. Partial text and length stops
remain failed; the existing denied-review trailing-verdict exception is preserved.
Exact-head verdict validity remains the seat layer's responsibility (D-4515, rule 3.26).
