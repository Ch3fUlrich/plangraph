use super::*;

// not-applicable: output and parsed-question redaction already exist on this lane.
#[test]
fn exception_keeper_payloads_and_question_exclude_harness_secret() {
    use crate::keeper::{AttemptReport, ExecId, HttpKeeper, Keeper};
    use pg_worker_core::packet::ParkPacket;
    let secret = ["sk-", "exceptionseed0123456789abcdef"].concat();
    assert!(!pg_worker_core::redact::redact(&secret).contains(&secret));
    for mode in ["clean", "failed", "cancelled", "timeout", "question"] {
        let (r, base) = repo();
        let root = tempfile::tempdir().unwrap();
        let inp = input(
            claim(&r.path().to_string_lossy(), &base, &["out.txt"], "true"),
            1,
            root.path(),
        );
        let ending = match mode {
            "clean" => "echo hi > out.txt".to_owned(),
            "failed" => "exit 1".to_owned(),
            "question" => format!(
                "echo 'PG_QUESTION: {{\"text\":\"{secret}\",\"default\":\"{secret}\",\"risk_tags\":[\"{secret}\"]}}'"
            ),
            _ => "sleep 5".to_owned(),
        };
        let script = format!("echo '{secret}'; echo '{secret}' >&2; {ending}");
        let harness = Fake::new(Box::leak(script.into_boxed_str()));
        let mut cfg = tiny();
        if mode == "timeout" {
            cfg.start_to_close = Duration::from_millis(300);
        }
        let started = std::time::Instant::now();
        let result = run_attempt_with(Some(&harness), &cfg, &Patterns::default(), &inp, &|| {
            mode == "cancelled" && started.elapsed() > Duration::from_millis(300)
        }).unwrap();
        assert_eq!(result.exit_class, match mode {
            "clean" => ExitClass::Ok,
            "failed" => ExitClass::WorkerCrash,
            "cancelled" => ExitClass::Cancelled,
            "timeout" => ExitClass::Timeout,
            _ => ExitClass::NeedsInput,
        });
        assert!(!serde_json::to_string(&result).unwrap().contains(&secret));
        let (url, server) = crate::keeper::tests::serve_n(
            if mode == "question" { 3 } else { 2 },
            |req| {
                if req.path == "/v1/ask_question" {
                    (200, "{\"id\":1}".into())
                } else {
                    (204, String::new())
                }
            },
        );
        let keeper = HttpKeeper::new(&url);
        let exec = ExecId::default();
        keeper.report_attempt(&AttemptReport {
            card: inp.claim.card.clone(), n: 1, leg: inp.leg.id.clone(),
            exit_class: result.exit_class, diff_hash: result.diff_hash.clone(),
            check_tail: result.check_tail.clone(),
        }, &exec).unwrap();
        if mode == "clean" {
            keeper.complete(&inp.claim.card, &result.head, &exec).unwrap();
        } else {
            keeper.park(&ParkPacket {
                card: inp.claim.card.clone(), tier: inp.leg.tier,
                exit_class: result.exit_class, check_tail: result.output_tail.clone(),
            }, &exec).unwrap();
        }
        if mode == "question" {
            keeper.ask_question(&inp.claim.card, result.question.as_ref().unwrap(), &exec).unwrap();
        }
        for request in server.join().unwrap() {
            assert!(!request.body.contains(&secret), "{mode}: {}", request.body);
        }
    }
}

// not-applicable: ownership, allowlisting and empty-claim refusal already exist.
#[test]
fn exception_activity_entry_owner_and_empty_claim_do_not_start_work() {
    use crate::keeper::StubKeeper;
    struct Held(std::sync::Barrier, std::sync::Barrier, Fake);
    impl Harness for Held {
        fn name(&self) -> &str { "held" }
        fn command(&self, run: &HarnessRun) -> Command {
            self.0.wait();
            self.1.wait();
            self.2.command(run)
        }
    }
    fn refused(error: temporalio_sdk::activities::ActivityError, kind: &str, key: &str) {
        let temporalio_sdk::activities::ActivityError::Application(failure) = error else {
            panic!("expected application failure")
        };
        assert!(failure.is_non_retryable());
        assert_eq!(failure.type_name(), Some(kind));
        assert!(format!("{failure:?}").contains(key));
    }
    let (r, base) = repo();
    let root = tempfile::tempdir().unwrap();
    let mut inp = input(claim(&r.path().to_string_lossy(), &base, &["out.txt"], "true"), 1, root.path());
    inp.claim.card = "exception-owner".into();
    let cfg = Config { card_allowlist: Some([inp.claim.card.clone()].into()), ..tiny() };
    let patterns = Patterns::default();
    let held = Held(std::sync::Barrier::new(2), std::sync::Barrier::new(2), Fake::new("echo hi > out.txt"));
    std::thread::scope(|scope| {
        let first = scope.spawn(|| run_attempt_with(Some(&held), &cfg, &patterns, &inp, &|| false));
        held.0.wait();
        let second = run_attempt_with(Some(&held), &cfg, &patterns, &inp, &|| false);
        held.1.wait();
        refused(crate::card_admit::attempt_error(second.unwrap_err()), "card_admission_refused", &inp.claim.card);
        first.join().unwrap().unwrap();
    });
    inp.n += 1;
    let harness = Fake::new("echo hi > out.txt");
    assert!(run_attempt_with(Some(&harness), &cfg, &patterns, &inp, &|| false).is_ok());
    let empty = StubKeeper::default();
    let unstarted = Fake::new("exit 99");
    let check = root.path().join("check-started");
    inp.claim.check_cmd = format!("touch '{}'", check.display());
    let result = crate::activities::admitted_claim(&empty, &cfg, &inp.claim.card)
        .and_then(|claim| {
            inp.claim = claim.unwrap();
            run_attempt_with(Some(&unstarted), &cfg, &patterns, &inp, &|| false)
                .map_err(crate::card_admit::attempt_error)
        });
    refused(result.unwrap_err(), "card_not_claimable", &inp.claim.card);
    assert_eq!(unstarted.runs(), 0);
    assert!(!check.exists());
    let unlisted = "exception-unlisted";
    refused(crate::activities::admitted_claim(&empty, &cfg, unlisted).unwrap_err(), "card_not_allowlisted", unlisted);
    assert_eq!(empty.claim_card_calls().len(), 1);
}

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
