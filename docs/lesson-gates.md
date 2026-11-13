# Spawner lesson gates

Gateway routes and explicit launches refuse with `gateway not configured` before creating a child when the effective base URL or client credential is absent. Set `PLANGRAPH_GATEWAY_KEY_FILE` to a private client key file, or use the established credential resolver.

Prompt-only review legs always receive the card diff inline, including explicit document-mode overrides. A missing diff refuses the launch. Completed prompt-only records whose dispatched `prompt.txt` lacks patch data have `verdict_status=void` and `verdict_void_reason=prompt_only_no_diff`; no review evidence is published.

`pg-spawnd review-probe` schedules tiny OK-answer probes for every active review route leg, including booked-down legs for recovery. It uses the isolated probe bridge and shared admission store. A failed review answer books the leg down immediately. MiMo review launches and probes disable thinking from their first request (`reasoning_cap=0` in the run record). Missing management credentials produce one routing-outbox notice per outage.

Implement cards bind the requested `writer_family` (default: the first writer
family in operator order) before availability checks. Only boolean
`allow_family_fallthrough: true` permits another writer family. Exhausting the
bound family returns `writer_family_unavailable`. Run records name
`launched_leg` even when that harness subsequently fails.

Session launcher configuration accepts `require_remote_control: true`. Required
launches and relaunches poll their own agent row for a nonempty
`bridgeSessionId`, bounded by the resolve budget (maximum sixty seconds). A
missing bridge returns `RC-MISSING`, retains the session id for cleanup, and
stores state `rc_missing`. This applies to the Linux and Windows session APIs.
The external fleet/terminal launchers must enable this policy and retain their
interactive `--remote-control NAME` workstation launch path.

`pg-spawnd rc-watch` compares RC-required live session records against a bounded `claude agents --json` query. Each missing bridge produces one durable notice to the recorded parent (or routing). Episode state survives timer restarts and rearms only when the bridge reconnects. Failed queries produce an error instead of treating unknown state as a disconnection.

`pg-spawnd run-watch` marks accepted running/queued runs `stalled_no_leg` when no PID, launched-leg record, or running/completed attempt exists within ten minutes (`PLANGRAPH_NO_LEG_TIMEOUT_S`). It writes one durable parent/routing notice and preserves the terminal state across ticks. A job id alone is queued bookkeeping and does not prove a harness launched.

Every run status/result/list read reconciles running records against the recorded
PID and start identity, and observes `live.log` age when available. A proven
missing, zombie, or reused PID reads as `exited` with reason `pid_gone`; unknown
process-query results retain the recorded state. These are read-only snapshots:
status polling does not rewrite records or race the completion writer.

Both spawner health transports use the effective child gateway check. Pure
planner enumeration retains its host-independent contract; service routing adds
the gateway and writer-family gates before making legs executable. MCP card
schemas expose `writer_family` and `allow_family_fallthrough`.

Prompt-only reviews may also receive an already supplied inline patch. Companion
evidence and Rust artifact publication enforce the same diff requirement.
Legacy uncapped MiMo attempts retain their bounded retry path; newly launched
MiMo reviews start capped and accept an already-disabled thinking configuration.

[Verification scope and baseline failures](lesson-gates-verification.md).
