Make the Windows descendant-stop test wait for the helper's grandchild creation
signal, with a 30-second hang guard. Keep both helpers alive until killed and
verify that a stop with suppressed signals detects the surviving descendant.
Launch the test grandchild without a console too: an extra console host in the
job can never satisfy an exact two-member wait. Assert exactly the child and
grandchild after readiness, naming every job member by PID and image on mismatch.
