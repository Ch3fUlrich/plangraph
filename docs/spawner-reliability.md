# Spawner reliability

## RL17: derived evidence and review seats

Evidence defaults off. `reliability.json` accepts `evidence` values off/shadow/on,
with `roles.<role>.evidence` overriding the default and
`PLANGRAPH_RELIABILITY_EVIDENCE` overriding configuration. Shadow and on persist
derived data only; neither enables routing. Operators roll back with off and may
delete the derived evidence directory while writers are stopped.

Rows use a separate stable lock file, tolerate an incomplete final line, and
refuse malformed complete lines. Outcome corrections use last physical row per
run/attempt before window filtering. Observations are separate from outcomes;
canaries never become health incidents. Existing unclassified attempts are
counted and skipped, not guessed from status. Legacy leg_events stay in the
adapter until P-R11. Review seats remain enabled even when evidence is off.
Only supported complete result frames supply rendered review text; missing or
unknown output produces HALT, never a manufactured witness or finish reason.

## RL12: backend/API foundation (partial)

The Unix-only `watch` module defines metadata-only hooks and results, an owned
child backend, and a clock-driven termination state machine. It is not called
by the harness. Commands are passed intact to the backend, which adds a fresh
process group and reuses the stall supervisor's spawn and signalling policy.
The stop path sends TERM once, escalates once after grace, reaps the root
without blocking, and requires ESRCH for emptiness. Permission errors cannot
prove a kill. Two seconds after escalation, unproven emptiness is `Survivors`.

This foundation does **not** complete SPAWNER-RL12-WATCH-SUPERVISE. Pending work:
bounded separate pipe draining and stdin delivery, the event loop and channel
aggregation, Blind shadow evaluation, source-priority selection, tree/CPU
sampling, calibration, EOF handling, and the required real-process matrix.
Owned groups remain signallable after leader reaping: Linux cannot reuse the
pgid while the group exists. Every signal first probes the guarded group;
the first ESRCH permanently latches emptiness, preventing later signalling.
EPERM is an error, not proof. Adopted children (RL29) retain identity checks.
`Survivors` carries the first signal, reap, or probe error kind, if any.
No production caller is enabled.

The remaining behavior and tests require a follow-up size allocation, rather
than removal of tests to meet the 400-production-line limit. Rollback is
`git revert`; RL14 must not call this partial API.
# Review artifact fix-round verification

Only exact `VERDICT: PASS` lines authorize passing evidence; noncanonical,
hyphenated, or later unknown verdicts fail closed. Raw review text is retained
(with secret redaction), including incomplete-evidence diagnostics and empty text.
The artifact regression test dry-runs `tools/seat_verdict.py` on canonical PASS,
HALT, and the quoted-HALT/markdown-ACCEPT pair. The external tool accepts that
last pair; the stricter artifact producer deliberately records HALT instead.
Temporarily restoring the old parser reproduced failures for the markdown pair
and `verdict: pass-through`; restoring the old body replacement failed the
missing-witness test. Dry runs keep the existing record schema without a new
unclassified-attempt counter; the existing placement-tools schema test covers it.

The reported HIGH CI excerpts are successful negative-path checks:
`pg-jail/src/lib.rs:229` (`missing_allow_path_is_rejected`),
`pg-jail/tests/escape.rs:332` (`fail_closed_missing_path`, including the expected
`pg-jail/src/main.rs:36` missing-command diagnostic),
`pg-spawnd/src/launch_tests.rs:152` (`lookup_rules_and_missing_program`),
`pg-spawnd/src/review_artifacts_tests.rs:75` (`missing_witness_or_finish_never_pass`),
and `pg-spawnd/src/runs_tests.rs:179` (`env_prompt_and_missing_carrier`).
