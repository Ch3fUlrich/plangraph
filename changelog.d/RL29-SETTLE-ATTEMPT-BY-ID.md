Fix run completion to settle the explicitly selected one-based attempt rather than the last attempt.
Preserve non-last attempt exit codes and reject unknown attempt ids without appending.
Launch books its attempt before execution and captures that id for completion; empty
arrays and malformed target entries are rejected without replacement. Successful
completion clears the attempts error marker. The supervisor writes terminal status
after the completion hook even when the hook rejects an invalid id. Recovery uses
its own indexed settlement bridge (not the spawn hook), covered with attempt 2 of 3.
