Set implement 25-minute stall detection while preserving 90-minute writer and 25-minute review hard walls, share hard deadlines across legs, support measured card-class widening, and end repeated tool loops or stdout-silent zero-CPU runs with named reasons.

Slow model turns use a card-configurable stuck_min, defaulting five minutes above the effective card/role stall window, including explicit stall_min overrides. Observable live child CLIs count as activity without extending hard wall deadlines; zombie children do not.

Tool-loop stops require six identical completed call/result pairs. Calls awaiting results do not count; id-less started/completed pairs count once, and repeated completions with an ID are ignored. All four structured harness transports retain immediate wake-up on proven loops.
