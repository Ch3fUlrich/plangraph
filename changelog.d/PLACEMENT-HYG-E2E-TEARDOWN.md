Fix local e2e teardown to record and reap fake opencode processes even when
their worker has exited or they have detached into a separate session.
Linux process identity checks protect unrelated processes from stale PID records.
Warn about SIGKILL survivors without aborting remaining cleanup; validate fake-script
recording and startup-failure cleanup with regression tests.
Share the fake writer's PID directory with the rig; tolerate directory lookup errors.
Exercise the real fixture directly and keep process mocks local to the helper module.
Do not guess PID directories for callers without recording configured; ignore failed
PID writes without changing fake answers, and isolate group-signal mocks from stdlib.
