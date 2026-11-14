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
Every native or legacy harness command uses stdin for task content, and child
environments discard known inherited or overlaid legacy task-prompt carriers before
launch-spec serialization, while retaining unrelated prompt-template/theme settings.
The [v1 registry](prompt-transport-v1.md) stages shared transport/reason data
and preserves the existing imports; S01 changes no launch behavior.

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

Gateway URL resolution is shared by child configuration and response-side witness
reads: OMNIROUTE_BASE_URL, then PLANGRAPH_GATEWAY_URL / the existing gateway_url
config mapping, then plangraph/omniroute-base-url in XDG_CONFIG_HOME (else
HOME/.config). This file fallback supports plain-shell spawn CLI runs without
launcher exports. File read failures mean unconfigured; no URL or key is logged.

The witness endpoint now uses the same effective child-base resolver, including
an existing `OPENCODE_CONFIG_CONTENT` omniroute settings baseURL. File-derived
endpoints must have an HTTP(S) scheme and hostname; invalid files stay
unconfigured and cannot trigger management-key reads or call-log requests.
