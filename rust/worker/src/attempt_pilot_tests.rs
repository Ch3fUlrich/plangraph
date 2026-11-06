use super::*;
#[test]
fn pilot_activity_owner_and_empty_claim() {
    struct Held(std::sync::Barrier, std::sync::Barrier, Fake);
    impl Harness for Held {
        fn name(&self) -> &str {
            "held"
        }
        fn command(&self, run: &HarnessRun) -> Command {
            self.0.wait();
            self.1.wait();
            self.2.command(run)
        }
    }
    let (r, base) = repo();
    let root = tempfile::tempdir().unwrap();
    let mut inp = input(
        claim(&r.path().to_string_lossy(), &base, &["out.txt"], "true"),
        1,
        root.path(),
    );
    inp.leg.harness = "held".into();
    let held = Arc::new(Held(
        std::sync::Barrier::new(2),
        std::sync::Barrier::new(2),
        Fake::new("echo hi > out.txt"),
    ));
    let cfg = Config {
        card_allowlist: Some([inp.claim.card.clone()].into()),
        ..tiny()
    };
    let first = std::thread::spawn({
        let held = held.clone();
        let cfg = cfg.clone();
        let inp = inp.clone();
        move || crate::card_admit::run_attempt(
            Some(&*held), &cfg, &Patterns::default(), &inp, &|| false,
        )
    });
    held.0.wait();
    let never = Fake::new("exit 99");
    let second = crate::card_admit::run_attempt(
        Some(&never), &cfg, &Patterns::default(), &inp, &|| false,
    );
    held.1.wait();
    crate::card_admit::tests::assert_failure(
        crate::card_admit::attempt_error(second.unwrap_err()),
        Some("card_admission_refused"), true,
    );
    assert_eq!(first.join().unwrap().unwrap().exit_class, ExitClass::Ok);
    crate::card_admit::tests::assert_failure(
        crate::activities::admitted_claim(
            &crate::keeper::StubKeeper::default(), &cfg, &inp.claim.card,
        ).unwrap_err(),
        Some("card_admission_refused"), true,
    );
    assert_eq!(never.runs(), 0);
}

#[test]
fn pilot_history_and_keeper_payloads_exclude_secrets() {
    use crate::keeper::AttemptReport;
    use pg_worker_core::packet::ParkPacket;
    let secret = ["sk-", "pilotseed0123456789abcdef"].concat();
    for (mode, ending, expected) in [
        ("clean", "echo hi > out.txt", ExitClass::Ok),
        ("failed", "exit 1", ExitClass::WorkerCrash),
        ("cancelled", "sleep 5", ExitClass::Cancelled),
        ("timeout", "sleep 5", ExitClass::Timeout),
        ("question", "", ExitClass::NeedsInput),
        ("split", "exit 1", ExitClass::WorkerCrash),
    ] {
        let (r, base) = repo();
        let root = tempfile::tempdir().unwrap();
        let inp = input(
            claim(&r.path().to_string_lossy(), &base, &["out.txt"], "true"),
            1,
            root.path(),
        );
        let script = match mode {
            "split" => format!("printf '[REDACTED]\\n0123456789abcdef\\n'; {ending}"),
            "question" => format!(
                "echo 'PG_QUESTION: {{\"text\":\"{secret}\",\"default\":\"{secret}\",\"risk_tags\":[\"{secret}\"]}}'"
            ),
            _ => format!("echo '{secret}'; echo '{secret}' >&2; {ending}"),
        };
        let h = Fake::new(Box::leak(script.into_boxed_str()));
        let mut cfg = tiny();
        if mode == "timeout" {
            cfg.start_to_close = Duration::from_millis(300);
        }
        let start = std::time::Instant::now();
        let result = crate::card_admit::run_attempt(
            Some(&h),
            &cfg,
            &Patterns::default(),
            &inp,
            &|| mode == "cancelled" && start.elapsed() > Duration::from_millis(300),
        )
        .unwrap();
        assert_eq!(result.exit_class, expected, "{mode}");
        let safe = |body: &str| {
            for fragment in ["sk-pilotseed", "0123456789abcdef"] {
                assert!(!body.contains(fragment), "{mode}: {body}");
            }
        };
        safe(&serde_json::to_string(&result).unwrap());
        safe(
            &serde_json::to_string(&AttemptReport {
                card: inp.claim.card.clone(),
                n: 1,
                leg: inp.leg.id.clone(),
                exit_class: result.exit_class,
                diff_hash: result.diff_hash.clone(),
                check_tail: result.check_tail.clone(),
            })
            .unwrap(),
        );
        safe(
            &ParkPacket {
                card: inp.claim.card.clone(),
                tier: inp.leg.tier,
                exit_class: result.exit_class,
                check_tail: result.output_tail.clone(),
            }
            .to_json(),
        );
    }
}
