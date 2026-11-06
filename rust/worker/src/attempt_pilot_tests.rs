use super::*;

// not-applicable: admission already exists; regression exercises the blocking
// entry called by the Temporal activity, including its error conversion.
#[test]
fn pilot_overlapping_activity_attempts() {
    struct Held(std::sync::Barrier, std::sync::Barrier);
    impl Harness for Held {
        fn name(&self) -> &str {
            "held"
        }
        fn command(&self, run: &HarnessRun) -> Command {
            self.0.wait();
            self.1.wait();
            Fake::new("echo hi > out.txt").command(run)
        }
    }
    let (r, base) = repo();
    let root = tempfile::tempdir().unwrap();
    let mut inp = input(
        claim(&r.path().to_string_lossy(), &base, &["out.txt"], "true"),
        1,
        root.path(),
    );
    inp.claim.card = "pilot-overlapping-activity".into();
    let cfg = tiny();
    let patterns = Patterns::default();
    let held = Held(std::sync::Barrier::new(2), std::sync::Barrier::new(2));
    std::thread::scope(|scope| {
        let first = scope.spawn(|| run_attempt_with(Some(&held), &cfg, &patterns, &inp, &|| false));
        held.0.wait();
        let second = run_attempt_with(Some(&held), &cfg, &patterns, &inp, &|| false);
        held.1.wait();
        let text = format!("{:?}", crate::card_admit::attempt_error(second.unwrap_err()));
        assert!(text.contains("card_admission_refused"), "{text}");
        assert!(text.contains(&inp.claim.card), "{text}");
        assert!(text.contains("non_retryable: true"), "{text}");
        first.join().unwrap().unwrap();
    });
    inp.n += 1;
    assert!(run_attempt_with(
        Some(&Fake::new("echo hi > out.txt")),
        &cfg,
        &patterns,
        &inp,
        &|| false,
    )
    .is_ok());
}
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

#[test]
fn pilot_attempt_owner_releases_on_error_and_panic() {
    struct Panics;
    impl Harness for Panics {
        fn name(&self) -> &str {
            "panic"
        }
        fn command(&self, _: &HarnessRun) -> Command {
            panic!("owner unwind")
        }
    }
    let (r, base) = repo();
    let root = tempfile::tempdir().unwrap();
    let mut inp = input(
        claim(&r.path().to_string_lossy(), &base, &["out.txt"], "true"),
        1,
        root.path(),
    );
    inp.claim.card = "pilot-owner-attempt".into();
    let cfg = tiny();
    let patterns = Patterns::default();
    assert!(
        std::panic::catch_unwind(|| run_attempt(&Panics, &cfg, &patterns, &inp, &|| false))
            .is_err()
    );
    inp.n += 1;
    let h = Fake::new("echo hi > out.txt");
    assert_eq!(
        run_attempt(&h, &cfg, &patterns, &inp, &|| false)
            .unwrap()
            .exit_class,
        ExitClass::Ok
    );
    inp.n += 1;
    let valid_base = inp.claim.base_sha.clone();
    inp.claim.base_sha = "missing-base".into();
    assert!(run_attempt(&h, &cfg, &patterns, &inp, &|| false).is_err());
    inp.claim.base_sha = valid_base;
    inp.n += 1;
    assert_eq!(
        run_attempt(&h, &cfg, &patterns, &inp, &|| false)
            .unwrap()
            .exit_class,
        ExitClass::Ok
    );
    inp.n += 1;
    assert_eq!(
        run_attempt(&h, &cfg, &patterns, &inp, &|| false)
            .unwrap()
            .exit_class,
        ExitClass::Ok
    );
}
