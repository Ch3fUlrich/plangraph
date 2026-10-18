# Recorded opencode 2.0.32 `run --format json` streams

Real recordings (only local paths replaced by `/work` and `/outside`): free-tier 429
(`provider.quota`, FreeUsageLimitError), a transport error, a text run, a tool run with
`step_finish` token counts, and a denied external-directory write.

Not recorded: a genuinely EMPTY run (no text, no tool call, no error). Every attempt to
provoke one produced text, a tool loop or a hang. The empty case is tested with a lone
`step_start` line instead; this is a known gap, not a recording.
