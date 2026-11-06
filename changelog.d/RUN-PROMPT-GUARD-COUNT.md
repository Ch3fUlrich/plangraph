## RUN-PROMPT-GUARD-COUNT (D-3931)

- Count exact spawn call sites against the prompt-record guard's allowlist;
  reject duplicate calls, removed calls, and over-counted exceptions.
- Isolate the seams probe assertion from production allowlist staleness and
  cover both fixes with regression fixtures. No production code changed.
