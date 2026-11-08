### Changed
- Claude review seats now expose only the selected built-in tool scope, isolate
  MCP to a packaged empty configuration, and disable slash-command discovery.
  Existing review permissions and all non-review command arguments are unchanged.
- Added offline `tools/judge_prompt.py` to assemble complete findings, PR diff,
  touched/cited file text within a byte budget, explicit cuts, and CI conclusions.
- Split runner launch, monitoring, and dispatch responsibilities into bounded modules.
