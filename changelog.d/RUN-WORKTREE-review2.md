Skip other-UID procfs entries before reading cwd, while preserving refusal for
unreadable same-UID processes. Ignore vanished processes with ENOENT or ESRCH.
