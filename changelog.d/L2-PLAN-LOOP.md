### Plan review loop

- Add hermetic command-boundary coverage for acceptance families, witness rules,
  material changes, minor diffs, stop conditions, caps, reply parsing, training
  material restrictions, and escalation.
- Prelaunch refusals no longer count as launched failures in stop evaluation.
- Launch high-tier discovery seats concurrently with blind prompts, share the
  round failure allowance, and allow interruption before result ingestion to retry.
Review-loop recovery fixes: driver and batch checks share the authoritative
manifest transaction and normalized policy. Minor fixes commit atomically;
retries recover stalled/ladder states without bypassing caps. Family aliases
and repeated snapshot ordering are normalized. Standard open questions are
listed, and automatic scheduling puts stacked review before scope cutting.
Hermetic coverage includes a SIGKILL subprocess and witness refusal.
