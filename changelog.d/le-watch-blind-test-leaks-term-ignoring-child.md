The worker-core watch_blind fixtures that run a TERM-ignoring busy loop now end it
by themselves: the loop exits when its harness process is gone (checked every 20000
turns) and after 60 s, and every fixture records its shell pid so a drop guard sends
SIGKILL to its group on every exit path. A test that SIGKILLs a harness mid-run
checks that no such child is left after 5 s.
