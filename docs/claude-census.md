# Claude census

Run `python tools/claude_usage.py --census --since 7d --json` with Cargo and
authenticated `gh`. `--projects` selects transcripts; `--until` pins the exclusive
UTC endpoint; `--since` accepts days or ISO date/time. `--merged-prs` accepts an offline array of `{number, mergedAt}`.

Weights come from `tools/claude_usage_policy.json`. Agent names/custom titles
identify router/routing, replan/replanning, L1-build/build-L1, L1/L1-other and L2;
a numeric suffix is removed. Unknown names remain `unclassified`.
Subagents sharing parent message IDs (or declaring `forkContext: true`) are
forks; others are inferred fresh. Copied parent turns are excluded. This inference
cannot prove a fresh launch when inherited messages are absent from a transcript.
User records may declare a `trigger`; otherwise beat prefixes identify beats,
work prefixes identify work, and other user text is msg-in. Tool results after
coordination tools count as coordination loops; other continuations count as work.
Repeated message IDs replace usage/time and retain their initiating trigger.

Role costs include unmerged work and divide by all repository PRs merged in the
same window. Zero merges produce null ratios. Missing inputs/gh errors fail closed;
malformed records are counted. No transcript content or machine identity is printed.
The guard flags three complete UTC days below 58 merges; operators revert the cut.
It excludes partial days and does not mutate Git. First live evidence is post-merge.
