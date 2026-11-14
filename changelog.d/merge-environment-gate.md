Pre-push now refuses invalid commit trailers before scrub or preflight, with an
explicit offline notice when the open-PR merge probe is unavailable.
Browser inputs select browser coverage; full-tier PRs run every suite and gate
Windows/browser results. Batch landing checks exact-head Actions jobs and
records rerun attempts in the PR body.
Batch landing freezes on missing, pending, red or cancelled main CI; the
head-bound GO-row main-red-fix exception is explicit and logged.
Seat cleanup fixtures use isolated, unsigned Git plumbing and no Windows console
host; a signing-poison regression and 50-iteration cache-survival stress test
retain the original reset assertions.
Review fixes preserve PR body contents when recording reruns, re-run tiering on
ready/retarget events, reject malformed open-PR evidence, and update legacy
hook/shadow-gate fixtures for the browser and trailer requirements.
