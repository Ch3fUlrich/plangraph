# Closed-stdin capture evidence

Recorded 2026-11-03 using the real `opencode v2.1.0` binary:

```sh
opencode run --standalone --format json --title plangraph-job-x --model opencode/deepseek-v4.1-flash-free "Read hello.txt using the read tool."
```

Fresh isolated HOME and XDG config/data/cache/state directories; working directory
contained `hello.txt` with `hello` and a newline. OPENCODE_CONFIG named a file
containing `{}`; inherited OPENCODE/XDG settings and secret-shaped environment
variables were excluded. The same prompt plus newline was written to piped stdin,
then stdin was closed immediately. No inherited stdin or existing session.

A selector drained stdout/stderr concurrently. Monotonic offsets were measured
from immediately before process launch for every received line (lines in the same
read share its offset); `ARRIVAL_MS` preserves all 17 stdout lines in order.
Exit 0 at 26741 ms; no stderr. First Request at 4001 ms; first successful Read at
6266 ms; last text at 26573 ms. Multiple separated arrivals before exit establish
that this recording was not exit-buffered. This is one observation, not a timing
guarantee or permission-policy validation; several reads failed before succeeding.

`CLOSED_STDIN` projects actual event types, exact tool names and statuses, and text
presence. IDs, wire timestamps, paths, tool input/output, metadata and message text
are removed; nonblank text becomes `OK`. No private raw log is retained.
Historical constants project the prescribed first 20 lines across seven streams:
two lines each for both quota errors, transport and text; five tool; six denied;
one empty. Their original versions are 2.0.32 and (empty only) 2.1.1.

Tests replay only; no CLI spawning. Version mismatch remains Blind; no pin update,
caller, live switch, provider classification or kill policy is introduced.
