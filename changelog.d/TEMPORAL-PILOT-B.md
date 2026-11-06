## TEMPORAL-PILOT-B (partial)

- Fail closed on invalid PG_CARD_ALLOWLIST files and refuse unlisted cards before claim or attempt setup.
- Redact helper attempt tails and harness questions; cover clean, failed, cancelled, timed-out, stderr, and wrapped-secret output.
- Exclude git/SSH files from harness auth staging and test confined harness/check children without inherited push credentials.
- Ownership verification remains blocked: the existing Claim response type exposes no owner id.
