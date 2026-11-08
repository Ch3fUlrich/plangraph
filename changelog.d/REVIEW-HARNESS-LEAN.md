### Changed
- Claude review seats now expose only the selected built-in tool scope, isolate
  MCP to a packaged empty configuration, and disable slash-command discovery.
  Existing review permissions and all non-review command arguments are unchanged.
- Added offline `tools/judge_prompt.py` to assemble complete findings, PR diff,
  touched/cited file text within a byte budget, explicit cuts, and CI conclusions.
- Claude fallback review leg defaults to prompt-only; inline-only retains built-ins.
  Gateway prompt-only reviews use a one-step tool-less OpenCode agent with MCP,
  skills and plugins denied. Runner structure is unchanged.
- No live token measurement was performed; startup savings depend on installed
  tools and are not asserted as a measured 25k reduction.
