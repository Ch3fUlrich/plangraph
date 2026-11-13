# Lean review harness

Claude reviews preserve permissions; built-ins remain Read/Grep/Glob/Bash for
tools and inline-only modes. Only prompt-only uses an empty `--tools` set.
Strict MCP uses packaged empty configuration; slash commands are disabled.
The Claude fallback review leg defaults to prompt-only. Existing inline-only
legs retain their meaning. Unknown harness/model pairs default to tools.

Gateway prompt-only reviews launch OpenCode standalone with a one-step agent
(tools are removed on its final step), deny-all permissions, and policies
denying tools, MCP, skills and plugins. `run` is non-interactive and does not
dispatch slash commands. Non-review commands retain their existing behavior.
The explicit config overlay is stored in a private temporary JSON file, not
argv or an `env` command. A portable Python launcher sets `OPENCODE_CONFIG`,
removes inherited `OPENCODE_CONFIG_CONTENT`, forwards prompt stdin and exit status,
and deletes the file after exit; unlaunched commands are cleaned at parent exit.
Config size does not grow the command line, including on Windows.
Stdin task transport is allowlisted for OpenCode (including its prompt-only launcher),
Claude print mode, agy, qodercli, Codex, Gemini and Qwen. Other helpers keep argv
without prompt stdin; malformed known stdin modes refuse. Duplicate carriers refuse as
`stdin_prompt_argv_conflict`, including short whole-text metadata carriers and nonempty agy/Qoder `-p` values; unsupported operands use `stdin_prompt_command_unknown`. Metadata substring detection has the same 16-byte floor in Python and Rust; shorter matches require whitespace boundaries, so `1` in `plangraph-job-1a2b3c4d` or `x` in `fixture/model` is not a carrier. The shared registry and legacy `clients` exports stay aligned. Child
environments discard known inherited or overlaid legacy task-prompt carriers before
launch-spec serialization, while retaining unrelated prompt-template/theme settings.

`tools/judge_prompt.py` assembles offline findings, diff, CI and whole file
bodies, prioritizing cited files under the byte budget. Its default prompt-only
brief says cut files are unavailable. `--tool-mode tools` permits read-only
follow-up; `inline-only` reads only the spawner-inlined diff, with no tools.
That brief does not advertise Claude's retained inline-only built-ins.
The offline config-schema test uses `jsonschema>=4` from `requirements-ci.txt`.

Live startup-token savings: not measured. No live measurement run IDs exist in
the repository's records, so a fixed 25k saving is not claimed.
Open step: three-run before/after token probe with recorded run IDs and token
counts for each run. Runner structure is unchanged.
