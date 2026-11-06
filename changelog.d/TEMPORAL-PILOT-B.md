## TEMPORAL-PILOT-B (partial)

- Fail closed on invalid PG_CARD_ALLOWLIST files and refuse unlisted cards before claim or attempt setup.
- Redact helper attempt tails and harness questions; cover clean, failed, cancelled, timed-out, stderr, and wrapped-secret output.
- Exclude git/SSH files from harness auth staging and test confined harness/check children without inherited push credentials.
- Refuse unavailable claims and guard same-card attempts within the worker; cross-worker arbitration belongs to the keeper.
- Exercise overlapping attempts through the blocking activity entry and empty-claim refusal; cover a staged operator GitHub credential helper being absent from confined children.
