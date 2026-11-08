Fix acknowledged remote spawns with unreadable or missing-ID receipts: retain an
unknown-status tracking record on the selected host instead of retrying elsewhere.
Default missing remote receipt statuses to unknown so saved records remain visible
through ps() and resource inspection. Existing LOW findings remain documented risks.
