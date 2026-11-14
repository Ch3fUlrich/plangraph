Make the Windows descendant-stop test wait for the helper's grandchild creation
signal, with a 30-second hang guard. Keep both helpers alive until killed and
verify that a stop with suppressed signals detects the surviving descendant.
