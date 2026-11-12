### Changed
- Claude review seats now expose only the selected built-in tool scope, isolate
  MCP to a packaged empty configuration, and disable slash-command discovery.
  Existing review permissions and all non-review command arguments are unchanged.
- Added offline `tools/judge_prompt.py` to assemble complete findings, PR diff,
  touched/cited file text within a byte budget, explicit cuts, and CI conclusions.
- Claude fallback review leg defaults to prompt-only; inline-only retains built-ins.
  Gateway prompt-only reviews use a one-step tool-less OpenCode agent with MCP,
  skills and plugins denied. Runner structure is unchanged.
- The three-run before/after token probe performed outside this diff on
  2026-11-08 measured at least 25k fewer tokens per review on all three runs.
- Judge briefs retain evidence for command-time rendering from the leg's mode.
  Missing or malformed inline OpenCode config falls back to tools without
  replacing discovered provider/gateway settings.
- Refusal dispatch regression coverage now exercises the prompt-only Claude
  fallback while preserving the terminal refusal and notification assertions.
