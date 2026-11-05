Added Unix event supervision with bounded stdin/output handling, Blind byte-stall enforcement, confirm-window decisions, and bounded process-group kill proof; harness routing remains unchanged until RL14.
Owned groups remain signallable after leader exit; latch ESRCH, guard emptiness probes, and report the first teardown error with survivors.
Review fixes gate the Unix supervisor explicitly, latch Blind before draining provider errors, preserve successful exits during pipe-tail draining, and retain transient probe errors in survivor reports.
Seat fixes explicitly gate Blind tests on Unix (the public module was already gated), require a wall timeout before spawning, and cancel/join nonblocking stdin writers to release prompts on teardown.
