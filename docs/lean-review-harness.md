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

`tools/judge_prompt.py` assembles offline findings, diff, CI and whole file
bodies, prioritizing cited files under the byte budget. Its default prompt-only
brief says cut files are unavailable. `--tool-mode tools` or `inline-only`
permits read-only follow-up. No live startup-token measurement was performed;
a fixed 25k saving is not claimed. Runner structure is unchanged.
