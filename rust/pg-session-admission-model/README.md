# Session admission model

`Model::apply` is the admission transition table. It models the lock as an
atomic transition, and models scope attachment separately from the claim write
so the crash between them can be replayed.

| Actor | Transition inputs |
| --- | --- |
| Execution host | Accept, take the start right, and bind a positively identified launched job and process. |
| Session hook | Authorize the exact bootstrap command, attach or verify the scope, authorize whole Reads, and verify returned bytes against the immutable manifest. |
| Session commands | Independently verify caller identity, claim from starting, and record a receipt for the current conversation. |
| Resolver | Publish only with verified current-generation evidence, and fence claims when recovery authorizes revocation or failure. |
| Session lifecycle | Invalidate unpublished Read and receipt evidence when the conversation changes. Apply the ready lock before allowing published work. |

Job and process identity, including PID reuse protection, are observations from
the execution host. Scope attachment and conversation verification stand for
the systemd and harness operations whose availability must be proved by parity
fixtures. Tool events represent parsed exact commands and correlated harness
events. `PostRead` compares the complete text delivered to the conversation.
Guidance settings affect the guidance result while admission bookkeeping
still runs. Out-of-process work is denied before publication.

The exhaustive test explores up to 15 events from the test's alphabet for one
attempt, two callers, two task files and two conversation generations. The
recovery model separately explores reservations, settlement evidence and
watchdog interleavings. `fixtures/transitions.json` version 1 contains the
admission replay vectors for R9-S2, including the expected result at every step.

See also the [session cutover model](../../docs/session-cutover-model.md) for the digest-wait cutover of an orchestrator role.
