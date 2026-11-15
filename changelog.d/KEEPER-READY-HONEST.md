Keeper ready now separates merged or outdated-premise work into needs_reconcile
with claim refusal reasons, and reports work on open PR heads as in_progress
with PR numbers. A shared cached origin index checks every candidate without
per-item git or GitHub calls; clean items remain startable.

Failed origin and PR-head fetches now expose stale/unknown status with age and
reason in ready JSON and one text warning, without classifying from stale facts.
Text output separates indented reconciliation, in-progress and unknown sections
from startable rows. Ready and claim share refreshed merge PR metadata.

Claim now fails closed when the refreshed work index is unverified, with the
existing audited force-stale override. `ready --needs-reconcile` prints only
dropped items and their reconciliation reasons in text or JSON.
