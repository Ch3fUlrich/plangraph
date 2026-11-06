# OpenCode termination after an answer (SPAWNER-EXIT-OUTCOME)

The supplied 40-run table (`~/code/briefs/deepseek-runs.json`) records
`run-a17c26a8` (605 s) and `run-5cf0d8e9` (435 s) as exit 1 with
`opencode_tools_denied`, `harness_failed`, and `has_verdict: true`.
Both have no configured stall or maximum duration. Their stderr contains a
build banner, `Error: terminated`, then another build banner. This is not
evidence of a spawner stall kill, nor is elapsed duration a fixed timeout proof.

The adapter invokes `opencode run --standalone`, feeds the prompt on stdin,
and waits for process collection before assigning an outcome. Terminal text
arriving on stdout is not equivalent to the run loop reaching a terminal
answer: OpenCode exposes separate text, step_start, and step_finish events
(also recognized by `rust/worker-core/src/events/opencode.rs`). Another step
can occur after text emission. Most likely the harness continued processing
after emitting the verdict and a subsequent request/stream terminated. The
banner ordering alone cannot prove which request failed, or even whether the
second banner names a new provider request. A provider connection failure is
possible; the table contains neither provider traces nor completion frames
and cannot establish the underlying cause or retrospectively certify answers.

The existing runner contract either marks nonzero exits `harness_failed` or promotes
an exact trailing seat verdict string to `completed`. Neither expresses the
transport distinction. Launches now request JSON events. A nonempty text answer
with step_finish reason `stop`, followed by exit 1 and the exact normalized
`Error: terminated` diagnostic, gets status/event `terminated_after_answer`.
The nonzero exit and stderr remain available; stdout exposes the completed
answer to callers. A later unfinished text answer invalidates the earlier
completion. Plain text, truncated JSON, and length/tool-call finishes provide
no completion evidence for the new outcome. The existing denied-review exact
trailing verdict exception remains unchanged for compatibility; the new
structured-answer classifier does not interpret seat verdicts or exact-head validity.
