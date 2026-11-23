# Spawner prompt rules

Build each role prompt in this order. Start with the stable harness header, role
skill, rules and gates so retries share a byte-identical prefix. Follow with
changing run evidence and context. Put the task and its output contract last.
Keep the review diff before the closing verdict reminder, including on grouped
inline seats. A tool-less seat's reminder follows the same verdict contract as
its brief.

`render_fixer_brief` in `src/plangraph/routing_support/fixer.py` numbers every
finding: each must be fixed, refuted by a named test, or reported as `not done:`
prose with the reason. Never echo an unchecked checklist or use a `Remaining gaps`
section for terminal `not done:` items. Run affected tests only
(`python3 tools/affected_tests.py --base origin/main --run`); COMMIT before finishing.

Retry bounds are not multiplied:
`src/plangraph/settings.py:ReviewMethod.max_rounds` (8, the ceiling of the stop rule in `rust/pg-spawnd/src/round_progress.rs`) is advisory, not an enforced
run cap; `src/plangraph/planner/routing.py:ATTEMPT_BUDGET` (3) feeds
`next_after_failure`'s per-tier retry/promotion policy, but the spawner's
`src/plangraph/spawner/service.py:_spawn` walks `plan.legs` once (no numeric
fallthrough constant), stopping on a non-provider/non-unavailable result or
`no_fallthrough`. Thus leg exhaustion or that earlier stop ends this spawn first,
not the advisory round cap.

## Prompt delivery

Every harness and helper receives the prompt by stdin or file, never argv, for
all prompt sizes on every OS (D-5233). Python's Linux OOM child inherits the stdin
pipe; pg-spawnd uses the same launch-spec transport.
Child env construction drops only known legacy task carriers (`PROMPT`, `PROMPT_TEXT`,
`PROMPT_SUFFIX`, `TASK_PROMPT`, `PLANGRAPH_TASK_PROMPT`, case-insensitive), including
overlays; unrelated shell/theme/template configuration survives. Rust refuses exact
prompt carriers and embedded prompts of at least 16 bytes in argv/env, without
mistaking short incidental substrings of flags or model names for task transport.
SSH overflow passes a staged UTF-8 file to the real spawn CLI's `--prompt-file`
option, preserving trailing newlines without shell command substitution.
Request-budget refusals keep the launch adapter's exit status in their run record;
pre-exec refusals without an exit code also retain the named `prompt_too_large` reason.
Qoder retains an explicit empty `-p` value with `--input-format text`; missing prompt flags fail with
`stdin_prompt_flag_missing` in launch-spec refusals and Python run records. All agy commands use
`--input-format stream-json --output-format stream-json -p ""`, with
one UTF-8 NDJSON user event on stdin followed by EOF. The last result's
`response` supplies the answer and verdict; an ERROR or missing result fails
explicitly, without publishing an unsuccessful response as an answer.
OpenCode, Claude, Gemini and Qwen read text stdin; Codex uses `exec -`. Fleet sessions
also read stdin; SSH overflow uses `--prompt-file` without shell expansion. Assembled
prompts exceeding the leg budget (three chars/token) record `prompt_too_large`.
Rejected stdin input or an unavailable/closed input pipe fails the run explicitly;
the watchdog never retries a rejected payload as an empty prompt. Gateway config
size refusal leaves the caller's environment unchanged.

## Role header and role skill (rule 3.57)

Every implement and review prompt starts with a role header naming the agent's own
harness (`src/plangraph/config/role_headers.json`, also embedded by the native writer),
then the role skill (`batch-writer` for a batch brief, `implement`, or `review`) read
byte-identical from the run checkout's `origin/main` (the working-tree copy of the same file when no
`origin/main` exists; if neither is readable the prompt carries the header only), then the caller's brief. A brief
that already starts with the skill text gets it once.
The native writer places its fixed gate and Cargo rules between the skill and
the caller's brief.

A review leg whose catalog `tool_mode` is `inline-only` gets the `review_inline` header and
no skill text: the header holds the inline rules (no tools, the change is quoted in the
prompt; a validated finding is a quoted diff line with `file:line`; CI evidence is named
instead of run; the severity scale, the output lines and the verdict are always given),
because the review skill demands test runs and file reads and `origin/main` may serve a
skill older than the header. The section "Inline seats" of `skills/review` states the same
rules for readers. An inline diff over the leg's request limit is refused with
`prompt_too_large: inline_diff_oversized`, never elided. The tool mode applies to the whole brief:
the native writer starts its review prompt with a `[tools-only]` block followed by
a `[review-skill]` block. Inline composition drops both and keeps the versioned brief
and item text verbatim. Tools legs keep both blocks once. Closing markers are reserved
in their respective blocks. Marker text in the brief or in quoted findings stays intact.
