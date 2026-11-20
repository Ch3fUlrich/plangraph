Isolate the native writer integration fixture from installed disk and overflow
placement policy, which can return a queued/refused run without a local task.
Retain lifecycle and cancellation assertions, report the run record when task
registration is absent, and cover restrictive host policy in a Rust regression.
Run the native writer integration tests under Python 3.14 in CI after the Rust
suite under Python 3.12. Both 3.12.3 and 3.14.4 passed locally; the reported dell
host failure was not reproduced with default local policy.
