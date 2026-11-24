A native-codex writer turn that hits the Codex usage limit now ends the run at
once as quota_limited (exit 11, result QUOTA) instead of relaunching three times.
The limit and its reset time are booked in the leg state, the committed head is
pushed through the push gate, and the status line reads `QUOTA <reset>`.
