Fix run completion to settle the explicitly selected one-based attempt rather than the last attempt.
Preserve non-last attempt exit codes and reject unknown attempt ids without appending.
Launch books its attempt before execution and captures that id for completion.
Legacy records with no booked attempts normalize to attempt 1; other unknown ids
and malformed target entries are rejected without replacement. Successful completion
clears the attempts error marker to null (not key absence); no production reader
uses key presence as an error signal. A rejected hook does not prevent a subsequent
record terminal transition. Recovery uses its own indexed settlement bridge (not
the spawn hook), covered with attempt 2 of 3: an unobserved exit code is null,
whereas the spawn hook preserves the selected attempt's existing exit code.
