use super::*;
fn secret_attempt(script: &str, timeout: bool, cancel: bool) {
    let (r, base) = repo();
    let root = tempfile::tempdir().unwrap();
    let inp = input(
        claim(&r.path().to_string_lossy(), &base, &["out.txt"], "true"),
        1,
        root.path(),
    );
    let script = script.replace(
        "SECRET",
        &format!("{}{}", "sk-", "pilotseed-0123456789abcdef"),
    );
    let h = Fake::new(Box::leak(script.into_boxed_str()));
    let mut cfg = tiny();
    if timeout {
        cfg.start_to_close = Duration::from_millis(300);
    }
    let start = std::time::Instant::now();
    let res = run_attempt(&h, &cfg, &Patterns::default(), &inp, &|| {
        cancel && start.elapsed() > Duration::from_millis(300)
    })
    .unwrap();
    let json = serde_json::to_string(&res).unwrap();
    assert!(!json.contains("pilotseed"), "{json}");
    assert!(!json.contains("0123456789abcdef"), "{json}");
    if timeout {
        assert_eq!(res.exit_class, ExitClass::Timeout);
    }
    if cancel {
        assert_eq!(res.exit_class, ExitClass::Cancelled);
    }
}

#[test]
fn pilot_clean_secret() {
    secret_attempt("echo SECRET; echo hi > out.txt", false, false);
}
#[test]
fn pilot_failed_secret() {
    secret_attempt("echo SECRET; exit 1", false, false);
}
#[test]
fn pilot_cancelled_secret() {
    secret_attempt("echo SECRET; sleep 5", false, true);
}
#[test]
fn pilot_timed_out_secret() {
    secret_attempt("echo SECRET; sleep 5", true, false);
}
#[test]
fn pilot_stderr_secret() {
    secret_attempt("echo SECRET >&2; exit 1", false, false);
}
#[test]
fn pilot_split_secret() {
    secret_attempt("printf 'SECRET\n0123456789abcdef\n'; exit 1", false, false);
}
