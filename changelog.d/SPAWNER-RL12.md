Added Unix event-supervisor API and clock-driven termination primitives; harness routing remains unchanged. Full RL12 supervision is not yet implemented.
Owned groups remain signallable after leader exit; latch ESRCH, guard emptiness probes, and report the first teardown error with survivors.
