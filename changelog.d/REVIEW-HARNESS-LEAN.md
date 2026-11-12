### Changed
- Claude review seats now expose only the selected built-in tool scope, isolate
  MCP to a packaged empty configuration, and disable slash-command discovery.
  Existing review permissions and all non-review command arguments are unchanged.
- Added offline `tools/judge_prompt.py` to assemble complete findings, PR diff,
  touched/cited file text within a byte budget, explicit cuts, and CI conclusions.
- Claude fallback review leg defaults to prompt-only; inline-only retains built-ins.
  Gateway prompt-only reviews use a one-step tool-less OpenCode agent with MCP,
  skills and plugins denied. Runner structure is unchanged.
- Live startup-token savings: not measured. No live measurement run IDs exist in
  the repository's records, so a fixed 25k saving is not claimed.
  Open step: three-run before/after token probe with recorded run IDs and token
  counts for each run.
- Judge briefs retain evidence for command-time rendering from the leg's mode.
  Missing or malformed inline OpenCode config falls back to tools without
  replacing discovered provider/gateway settings.
- Refusal dispatch regression coverage now exercises the prompt-only Claude
  fallback while preserving the terminal refusal and notification assertions.
- Inline-only judge briefs now advertise only the spawner-inlined diff, no tools.
  Offline config-schema tests declare `jsonschema>=4` in `requirements-ci.txt`.
- Gateway prompt-only config now travels through a private temporary file and a
  portable Python launcher, not argv or POSIX `env`; prompt stdin is retained,
  command length is independent of config size, and the file is cleaned on exit.
