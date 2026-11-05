Fix local e2e teardown to record and reap fake opencode processes even when
their worker has exited or they have detached into a separate session.
Linux process identity checks protect unrelated processes from stale PID records.
