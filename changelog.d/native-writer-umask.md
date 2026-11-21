Native writer, gate and push commands use umask 022. Merge-batch review fixtures set their own record permissions.
The native gate regression starts in a separate test process with umask 002.
