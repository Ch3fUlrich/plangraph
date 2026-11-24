# Launch recovery model

`Model::apply` is the transition table. Each event stands for one atomic
check-and-write or one observation by its listed actor. The model owns no
processes, files, SSH connections, or clocks.

| Actor | Transition inputs |
| --- | --- |
| Origin launcher | Reserve and forward the immutable spec, store the matching terminal reply, mirror before closing, and acknowledge. |
| Execution host | Receive or replay, allocate one start right, classify refusals, and resolve against local evidence. |
| Session and admission gate | Bind the launched job, claim while starting, and provide current-generation delivery proof. A conversation change invalidates uncommitted proof. |
| Watchdog | Persist the attempt before launch, resolve after interruption, confirm and promote the exact replacement, persist stop eligibility, and recheck before stopping. |

An open reservation pins placement. An open execution record excludes other
attempts for that seat at that host. Fleet-wide permit issuance across unrelated
origins and targets belongs to the registry. The model accepts positive job
binding, complete delivery and scope status as evidence from the respective host
adapters. A listing row with the same name is insufficient to establish linkage.

Publication staging and mirroring model idempotent writes as a group. The immutable
spec represents the fully staged inputs. Durable terminal records are retained in
this model for the entire trace. Garbage collection, name allocation, and
per-artifact filesystem behavior are outside the exploration bounds. The caller
supplies the watchdog's stop bound in beats and measures elapsed beats while all
three proofs hold.

The exhaustive test explores every ordering up to 13 events from its stated
alphabet for two attempts. One configuration shares a target and the other
shares an origin. The named traces exercise longer paths and additional evidence.
`fixtures/transitions.json` version 1 is the R9-S2 replay input. Each scenario
starts at a fresh attempt and gives expected results for each guarded event.

See also the [session cutover model](../../docs/session-cutover-model.md) for the digest-wait cutover of an orchestrator role.
