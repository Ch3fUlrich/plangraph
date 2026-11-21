# Spawner MiMo seats

Moved out of [SPAWNER.md](../SPAWNER.md) to keep it under its line cap (`tools/check_file_size.py`, `DOC_CAPS`).

## MiMo seat timeouts and verdict packaging (MIMO-VERDICT-TIMEOUTS)

For MiMo seats and all other `review` runs, `run_watchdog_limits.thresholds` sets
`hard_max=True`, including default caps: reviews never wait for a writer's first
edit and ignore a valid `hard_first_edit` opt-in. Card limits must be positive,
finite numbers (not booleans); invalid limits refuse the spawn. Without a card
`max_min`, an unset `PLANGRAPH_SPAWN_RUN_TIMEOUT` gives a 25-minute cap, an empty
value keeps the default 90-minute wall, and a positive numeric value sets seconds.
The package-wide `inline_diff_prompt` bound change was dropped to preserve main's
cost-context contract: `max_chars` measures only the diff and exact-fit diffs pass.
Oversized diffs keep `InlineDiffError`'s `diff too large for leg` text and diff-only
size, prefixed with `prompt_too_large`. Seat assembly maps that refusal to
`PromptBoundError` (`ContextError` and `InlineDiffError`) with `inline_diff_oversized`;
the base `InlineDiffError` is not itself a context/bound error. Whole-prompt admission retains `inline_prompt_oversized`
for oversized instructions/framing, both under the named `prompt_too_large`
reason regardless of `PLANGRAPH_CONTEXT_STRICT`. Neither truncates;
missing diff references, empty diffs and failed `git diff` calls fail explicitly.

In `opencode_answer.final_answer`, `allow_unfinished=True` accepts pending text
with no finish frame or after `tool-calls`, unless an earlier finish blocked it.
Reasons other than `stop` or `tool-calls`, including missing or unknown reasons,
fail closed: pending text is discarded and unfinished tails stay ineligible.
Only a `stop` with non-blank pending text clears that block; empty or
whitespace-only stops and tool calls cannot launder truncation into a verdict.
A completed `stop` answer remains eligible.

## Reasoning-only output exhaustion (MIMO-REASONING-OUTPUT-LIMIT)

Before decoding display stdout, `opencode_answer.record_steps` retains each
`step_finish` as an `opencode_step_finish` event with `finish_reason`,
`output_tokens` and `reasoning_tokens`; absent usage remains null, not zero.
Structured `output-limit` errors and the matching OpenCode stderr diagnostic
add `opencode_output_limit`. These are diagnostic events, not served-model witnesses. Review tool-denial validation and its defaults live in `spawner/opencode_isolation.py`. The `spawner.runner` gate wrapper passes its own `_DENIED_TOOLS`, `_REVIEW_OPENCODE_ENV_ALLOW` and `_opencode_managed_dir` at call time, preserving rebinding of those three dependencies. The wrapper also resolves the isolation implementation at call time; isolation internals such as `json`, `Path` and `Mapping` are not runner override points. Patching the shared `sys.platform` attribute still reaches managed-directory resolution, while replacing runner module objects is outside this contract. All managed-directory, permission and child-environment checks remain mandatory; only the runner wrapper supplies explicit dependency values. Direct isolation calls resolve omitted or `None` inputs from the isolation module at call time.
A final `length` step with output zero, or that error with no text/output,
adds `opencode_reasoning_exhausted`; later completed output is not retried.
Display-text decoding also protects plain/mixed stdout answers; diagnostic-only JSON, including blank separator lines, is not answer text.
The monitor's events reach `record.json` through either dispatch path.

Every supported MiMo OpenCode launch uses shared `opencode_answer.cap_reasoning` to set its own `providers.mimo.models[model].limit.output` to the catalog's 131072 combined reasoning/answer tokens, plus the per-child legacy output-max env, and caps `body.reasoning.max_tokens` (below the output limit) in shipped MiMo cards, rendered template, and runner launches unless thinking is already disabled. Model package overrides take precedence over the provider package; only the openai-compatible package is supported. Missing entries are never fabricated. Malformed config, limit/body objects, incompatible packages and invalid catalog caps refuse with named `reasoning_output_limit: ...` reasons before launch. Combos resolve before this gate; unknown MiMo IDs retain the named path refusal. Non-MiMo launches and the root config are unchanged. Structured usage counts survive record scrubbing only as nonnegative integers; token secrets stay masked.
For an OpenCode inline `review`, shared `SeatSpawnMixin._launch_leg` still retries
once with the same prompt, model and harness, never unchanged. MiMo v2.6 Flash
and Pro use model-local `body.thinking.type=disabled` (zero reasoning budget)
in the isolated child config on retry only; other roles do not disable thinking.
An already-disabled or unsupported control is unavailable, not an unchanged retry.
The failed attempt and its events stay in the record, with `reasoning_retry=1`
saved before the retry. Tool denial, fresh job isolation and the original review
wall deadline remain in force. No new retry loop or provider-health booking is added.
The retry requires more than 30 seconds of remaining wall budget. Base-service launches keep their original shielded monitor cancellation and ownership fields.
Repeated zero-output exhaustion, an unavailable cap, or no remaining retry budget
ends the run `failed` with terminal `reason: reasoning_output_limit`; no spare leg
or writer continuation runs. Cancellation, timeout and kill-incomplete still win.
Terminal status, reason, attempts and events are saved at the shared launch boundary before either caller returns; routed exhaustion keeps normal failure witness metadata, and both callers finalize `ended` and release monitor registrations in `finally`.
Normal output, nonzero-output truncation and non-review roles retain existing behavior except that base-service watchdog exits now retain one attempt row, matching routed dispatch; `terminal_record` adds no rows, retries do not duplicate watchdog rows, and cancellation rows are unchanged.
Hermetic coverage is in `tests/test_spawner_review_tools_inline.py` and `tests/test_spawner_runner_answer.py`, with Rust cap contracts in `rust/pg-spawnd/tests/mimo_output_cap.rs`; live seats remain an after-merge operator check.
