# OpenCode termination after an answer (SPAWNER-EXIT-OUTCOME)

The reproducible cases in `tests/test_spawner_runner_answer.py` model exit 1
after a stop-completed JSON answer, including denied-review verdicts and
writer answers. They also cover unfinished answers and unrelated errors.
An `Error: terminated` diagnostic alone is not evidence of a spawner stall
kill, nor is elapsed duration a fixed timeout proof.

The adapter invokes `opencode run --standalone`, feeds the prompt on stdin,
and waits for process collection before assigning an outcome. Terminal text
arriving on stdout is not equivalent to the run loop reaching a terminal
answer: OpenCode exposes separate text, step_start, and step_finish events
(also recognized by `rust/worker-core/src/events/opencode.rs`). Another step
can occur after text emission. A later request or stream may terminate after
an answer completes. Terminal banners alone cannot establish the underlying
provider failure or retrospectively certify answer completion.

The existing runner contract either marks nonzero exits `harness_failed` or promotes
an exact trailing seat verdict string to `completed`. Neither expresses the
transport distinction. Launches now request JSON events. A nonempty text answer
with step_finish reason `stop`, followed by exit 1 and the exact normalized
`Error: terminated` diagnostic, gets status/event `terminated_after_answer`.
The nonzero exit and stderr remain available; stdout exposes the completed
answer to callers. A later unfinished text answer invalidates the earlier
completion. Plain text, truncated JSON, and length/tool-call finishes provide
no completion evidence for the new outcome. The existing denied-review exact
trailing verdict exception is preserved for plain text, stop-completed JSON
answers, and pending JSON text without a stop frame; text after an explicit
non-stop finish is not promoted. The new
structured-answer classifier does not interpret seat verdicts or exact-head validity.
