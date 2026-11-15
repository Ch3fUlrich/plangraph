Claude prompt-only review seats now receive the shared inline Git diff or refuse
before launch with `no_diff_for_toolless_seat`. Explicit leg selection cannot
bypass diff delivery. Review answers containing `<parameter_missing>` without a
usable verdict fail with reason `parameter_missing`. Repository-tool seats retain
their existing behavior. Rust bridge regressions cover diff delivery, refusal,
final-attempt classification, and terminal-owner preservation.
