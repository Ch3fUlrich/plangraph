## Pytest deadlines

The default 120-second deadline covers setup, call and teardown. On POSIX,
an interruptible phase that times out reports `HUNG <nodeid>` with thread
stacks through pytest, so the xdist worker can run the next case. Teardown
gets a fresh deadline after an earlier phase times out. On platforms without
thread-directed signals (Windows), the watchdog interrupts the main thread
through `_thread.interrupt_main`, so a hung Python test still fails with
`HUNG <nodeid>`. That interrupt fires only at a bytecode boundary: a wait that
blocks outside the interpreter loop, such as `Event().wait()`, is not
interrupted until it returns, and the enclosing job or command provides the
stop. Child processes are reaped at the deadline on Linux only. The pre-push path module has a 360-second override
to accommodate the measured cold build of its shared train CLI fixture.
