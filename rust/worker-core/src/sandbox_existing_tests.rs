#[test]
fn allowlist_is_exact_names_only() {
    for n in ["PATH", "LANG", "TERM", "CARGO_HOME", "RUSTUP_HOME"] {
        assert!(is_passed(n) && is_passed_check(n), "{n} must pass");
    }
    for n in [
        // The harness sets this on its own Command; it survives via
        // `explicit`, it is not forwarded from the worker env.
        "OPENCODE_DISABLE_AUTOUPDATE",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "OPENCODE_API_KEY",
        "PG_KEEPER_URL",
        "TEMPORAL_API_KEY",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "HOME",
    ] {
        assert!(!is_passed(n), "{n} must not pass");
        assert!(!is_passed_check(n), "{n} must not reach a check child");
    }
}
