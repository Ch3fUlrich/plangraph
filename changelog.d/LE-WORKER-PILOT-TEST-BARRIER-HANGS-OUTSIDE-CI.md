The rust/worker test `pilot_activity_owner_and_empty_claim` no longer hangs when run outside CI.
Cause: a worker test run without `PG_JAIL=off` and `PG_TEST_ALLOW_UNJAILED=1` (set by CI and by
the fast checks, not by a plain `cargo test`) makes the first attempt end at once with the jail
refusal, so it never reached the two `Barrier(2)` waits and the test thread waited forever. The
hand-off is now a channel pair with a 30 s deadline: the test panics at once, naming the first
attempt's result and the two env settings, or after 30 s naming the stage that never arrived.
