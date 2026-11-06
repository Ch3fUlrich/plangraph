# Reviewer instructions

Use these priorities after the review preamble; keep the existing vector and
answer-tail contract. This is review guidance, not an additional admission gate.

## Evidence and triage

Open the reply with counts for important, nit, and pre-existing findings.
Report at most three nits; prioritize consequences over style preferences.
Do not use the nit cap to discard unresolved evidence requests.
Each finding carries tier (important|nit|pre-existing), class, file:line,
trigger, and consequence; distinguish severity (HIGH/MED/LOW) from tier.
A blocking finding must name a file:line inside the reviewed diff or touched
files and a concrete trigger. Without either, return it once to the same seat
for evidence; if still unsupported, record it LOW, never drop it.
Pre-existing findings never block, regardless of severity.
Label reasoned probes as unrun; absent execution evidence is not a failed test.
For HIGH design reviews, state the threat model: protected assets, trust
boundaries, attacker-controlled inputs, and the failure being prevented.

## What failure looks like, by area

- tests/: Test discrimination fails when a test stays green without the fix,
  reaches the wrong refusal, or checks an old snapshot instead of the result.
  Pin the intended branch and consequence, not merely an exception or log hit.
- rust/: Identity and scope fail when deduplication precedes filtering and
  discards another route's observation; probe equal keys across distinct scopes.
  Test discrimination fails when cleanup assertions omit surviving children or
  scope fixtures change two dimensions together and cannot isolate either.
- src/plangraph/keeper: Security boundaries fail when cached decision inputs
  admit a stale claim after a same-size, same-timestamp rewrite. Probe enforcement
  reads separately from display reads; do not infer freshness from metadata.
- src/plangraph/spawner: Error handling fails when malformed configuration or
  artifact publication errors escape the completion path. Probe empty overrides
  and invalid filesystem objects, and check the recorded terminal outcome.
- docs/: Contract drift fails when prose promises behavior no production caller
  implements or gives incompatible acceptance criteria. Trace the claim to its
  caller and a checkable outcome; do not treat a declaration as enforcement.

## Context limits and low-signal paths

A delta is not a complete module. Do not claim a function, import, constant,
callee signature, or runner wiring is missing merely because it is not shown.
Say instead: "Not visible in the supplied context; verify the definition or
call contract with the full touched file or a targeted probe."
Identify the touched file:line and the suspected trigger; apply the one-return
evidence rule above before treating that uncertainty as blocking.

Skip style-only review of changelog.d/ fragments and blank-line/comment-only
hunks in tests/run-tests.ps1; still check required entries and runner behavior.
Skip hand-review of upstream implementation in vendor/ and generated lockfile
contents such as rust/Cargo.lock. Review dependency changes, provenance,
licenses, size budgets, and integration at their maintained inputs instead.
Do not blanket-skip tests/, docs/, manifests, or security-filter fixtures.
