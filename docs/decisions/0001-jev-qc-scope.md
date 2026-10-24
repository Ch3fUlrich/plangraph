# Jev QC single-axis scope

## Context

Q1-Q5 describe separate review checks, but the keeper verdict exposes only a
single accept/reject decision. Inventing per-check labels would misstate evidence.

## Decision

Keep QC single-axis (`qc_accept`), observed at the production keeper verdict.
The outcome is the accepted rules verdict, not the worker execution result.
The production keeper invokes `plan_actions` with its store and writer principal
and an asynchronous, outcome-bearing observation hook. Other callers may inject
their own hooks without changing the rules plan.

## Consequences

No Q1-Q5 coverage is claimed. Routing observations belong to a numbered execution
attempt and execution outcomes fill only routing kinds for that attempt. Network
work is fire-and-forget and may be lost on process exit; it never delays decisions.
