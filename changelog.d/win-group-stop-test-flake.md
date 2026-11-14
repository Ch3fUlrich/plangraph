Make the Windows descendant-stop test wait for the helper's grandchild creation
signal, with a 30-second hang guard. Keep both helpers alive until killed and
verify that a stop with suppressed signals detects the surviving descendant.
CREATE_NO_WINDOW can add a console host to the job for each console process.
Require the child and grandchild after readiness, allowing at most two extra
members whose image paths end with \\System32\\conhost.exe (case-insensitively).
Name every job member by PID and image on mismatch, and verify all recorded
members, including console hosts, are gone after stopping the group.
