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

- Restore atomic ledger and yield exports after the manifest journal commit;
  loading repairs missing or differing projections after interruption.
- Complete the acceptance audit, checking each rule against test assertions:
  1. Four distinct families, aliases, unwitnessed findings, private eligibility,
     unknown witness policy keys, and operator decisions.
  2. Material invalidation and witnessed, unwitnessed, and rejected minor diffs.
  3. Exact cap output, refusal, extension, and counted versus uncounted rounds.
  4. Each stop condition, failed fact reports, and high-tier unverified disposal.
  5. Major verdict override, malformed blocks, truncation, and last attempt only.
  6. No degraded discovery, three-failure stall, exact output, and waiver guards.
  7. Monotonic ladder, exact exhaustion output, and past fixes in bundles.
  8. Prelaunch fall-through without attempts and launched failure accounting.
  9. Training material restrictions, zero-hit scrubbing, missing wordlists,
     and registry contributor/training load refusals.
- Audit the module-local rules: checkout directory refusal, unknown fix digest,
  major and oversize minor-freeze refusal, marker quoting, stripped identities,
  whole-plan bundles, real inline-only dry-run, exact review and fact-check argv,
  and every batch-check return path including explicit waiver permission.
