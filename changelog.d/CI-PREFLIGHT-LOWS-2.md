## CI preflight low-severity follow-up

Isolate preflight test tool overrides and preserve the running toolchain in the
missing-preflight hook test. Restore top-level test separators. Malformed Git
batch headers now refuse with one reason line instead of a traceback. Verified
that the shared hook Git helper already captures stderr for installed-hook failures.
