Serve captures: 2.2.5 and 2.0.32, stdout and stderr saved verbatim.
Command: `opencode serve --hostname 127.0.0.1 --port 0`.
Each invocation used a fresh scratch HOME and XDG config/data/cache/state
directories, a minimal environment, and OPENCODE_DISABLE_AUTOUPDATE=1.
OPENCODE_PASSWORD and OPENCODE_SERVER_PASSWORD were both set to the public
test value `fixture-only-not-a-secret` to suppress generated credentials.
After four seconds the scratch process group was killed and both pipes drained.
Both stderr files are intentionally empty. Tests also replay the captured
banner on stderr with ANSI coloring to cover stream/terminal variations.
