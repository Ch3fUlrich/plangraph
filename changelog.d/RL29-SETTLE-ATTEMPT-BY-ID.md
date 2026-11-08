Fix run completion to settle the explicitly selected one-based attempt rather than the last attempt.
Preserve non-last attempt exit codes and reject unknown attempt ids without appending.
Launch books its attempt before execution and captures that id for completion.
Legacy records lacking the attempts key normalize only id 1, marked
`legacy_normalized: true`; explicit empty arrays, other unknown ids,
and malformed target entries are rejected without replacement. Rejection changes
only attempts_error, before guard enforcement. RunRecord::create supplies an empty
array; the legacy regression loads a persisted missing-key record. Successful completion
clears the attempts error marker to null (not key absence); no production reader
uses key presence as an error signal. A rejected hook does not prevent a subsequent
record terminal transition. Recovery uses its own indexed settlement bridge (not
the spawn hook), covered with attempt 2 of 3: a pre-existing exit code of 7 is cleared to null,
whereas the spawn hook preserves the selected attempt's existing exit code.
Empty leg/route attribution skips the evidence row instead of blocking orphan settlement.
