# Recorded opencode `run --format json` streams

Real recordings (local paths replaced by `/work` and `/outside`; session, message and part ids replaced by synthetic `ses_syntheticNNN`-style ids): free-tier 429
(`provider.quota`, FreeUsageLimitError), a transport error, a text run, a tool run with
`step_finish` token counts, and a denied external-directory write.

`real-empty-run.jsonl` was recorded with opencode v2.1.1 (pinned) on 2026-10-23.
Command, run in a fresh empty temp dir:

```sh
opencode run --standalone --format json -m console-openrouter/liquid/lfm-2.5-2.6b:free "Respond with nothing. Emit no characters at all."
```

Exit 0; stdout is a lone `step_start` event, with no text/tool/error (1 of 5
prompts; the model is nondeterministic, other prompts gave text, tool loops or a
hang). Ids replaced by synthetic ids; no paths/user names present. The other
fixtures are from opencode 2.0.32.
