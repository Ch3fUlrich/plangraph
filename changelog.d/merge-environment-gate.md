Pre-push now refuses invalid commit trailers before scrub or preflight, with an
explicit offline notice when the open-PR merge probe is unavailable.
Browser inputs select browser coverage; full-tier PRs run every suite and gate
Windows/browser results. Batch landing checks exact-head Actions jobs and
records rerun attempts in the PR body.
Batch landing freezes on missing, pending, red or cancelled main CI; the
head-bound GO-row main-red-fix exception is explicit and logged.
