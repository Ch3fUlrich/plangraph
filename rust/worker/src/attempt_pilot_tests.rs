use super::*;

fn random_body() -> String {
    use std::io::Read;
    let mut bytes = [0u8; 16];
    std::fs::File::open("/dev/urandom").unwrap().read_exact(&mut bytes).unwrap();
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

fn excludes_secret_fragments(body: &str, secret: &str, floor: usize) -> bool {
    assert!(floor > 0 && floor <= secret.len(), "family floor exceeds fixture length");
    secret.as_bytes().windows(floor).all(|window| {
        !body.as_bytes().windows(floor).any(|candidate| candidate == window)
    })
}

#[test]
fn pilot_leak_oracle_rejects_every_interior_window() {
    let secret = ["ghp_", &random_body()].concat();
    let floor = pg_worker_core::GITHUB_TOKEN_BODY_FLOOR;
    for start in 0..=secret.len() - floor {
        for end in start + floor..=secret.len() {
            let leaked = &secret[start..end];
            assert!(!excludes_secret_fragments(leaked, &secret, floor));
        }
    }
    assert!(excludes_secret_fragments("[REDACTED]", &secret, floor));
    let short = &secret[5..5 + floor - 1];
    assert!(excludes_secret_fragments(short, &secret, floor));
}

#[test]
fn pilot_leak_guard_rejects_full_prefix_and_suffix_leaks() {
    let floor = pg_worker_core::GITHUB_TOKEN_BODY_FLOOR;
    let secret = ["ghp_", &random_body()].concat();
    assert!(floor <= secret.len(), "family floor exceeds fixture length");
    for leaked in [
        &secret[..],
        &secret[..floor],
        &secret[secret.len() - floor..],
    ] {
        let output = crate::output_redact::history_text(&format!("TOKEN={leaked}"));
        assert!(excludes_secret_fragments(&output, &secret, floor));
    }
    assert_eq!(crate::output_redact::history_text(&secret), "[REDACTED]");
}

#[test]
fn pilot_complete_token_redacts_repeated_suffix_line() {
    for prefix in [["gh", "p_"].concat(), ["s", "k-"].concat()] {
        let secret = [prefix.as_str(), &random_body()].concat();
        let suffix = &secret[secret.len() - 16..];
        for newline in ["\n", "\r\n"] {
            let text = format!("{secret}{newline}{suffix}");
            assert_eq!(
                crate::output_redact::history_text(&text),
                "[REDACTED]"
            );
        }
    }
}

#[test]
fn pilot_leak_guard_explains_oversized_floor() {
    let short = ["gh", "p_", "abc"].concat();
    assert_eq!(crate::output_redact::history_text(&short), short);
    assert!(short.len() < pg_worker_core::GITHUB_TOKEN_BODY_FLOOR);
    let oversized = std::panic::catch_unwind(|| {
        excludes_secret_fragments("[REDACTED]", &short, short.len() + 1)
    }).expect_err("an oversized oracle floor must fail");
    let message = oversized.downcast_ref::<&str>().copied()
        .or_else(|| oversized.downcast_ref::<String>().map(String::as_str));
    assert_eq!(message, Some("family floor exceeds fixture length"));
}
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
        move || {
            crate::card_admit::run_attempt(Some(&*held), &cfg, &Patterns::default(), &inp, &|| {
                false
            })
        }
    });
    held.0.wait();
    let never = Fake::new("exit 99");
    let second =
        crate::card_admit::run_attempt(Some(&never), &cfg, &Patterns::default(), &inp, &|| false);
    held.1.wait();
    let error = second.unwrap_err();
    assert!(
        matches!(&error, crate::card_admit::EntryError::Attempt(AttemptError::Admission(crate::AdmissionReason::Overlap(card))) if card == &inp.claim.card)
    );
    crate::card_admit::tests::assert_failure(
        crate::card_admit::attempt_error(error),
        Some("card_attempt_overlap"),
        true,
    );
    assert_eq!(first.join().unwrap().unwrap().exit_class, ExitClass::Ok);
    crate::card_admit::tests::assert_failure(
        crate::activities::admitted_claim(
            &crate::keeper::StubKeeper::default(),
            &cfg,
            &inp.claim.card,
        )
        .unwrap_err(),
        Some("card_not_claimable"),
        true,
    );
    assert_eq!(never.runs(), 0);
}

#[test]
fn pilot_history_and_keeper_payloads_exclude_secrets_and_preserve_below_floor_second_line() {
    use crate::keeper::AttemptReport;
    use pg_worker_core::packet::ParkPacket;
    let floor = pg_worker_core::GITHUB_TOKEN_BODY_FLOOR;
    for prefix in [["gh", "p_"].concat(), ["s", "k-"].concat()] {
        let secret = [prefix.as_str(), &random_body()].concat();
        let fragment = &secret[secret.len() - 16..];
        assert!(floor <= secret.len(), "family floor exceeds fixture length");
        for (mode, ending, expected) in [
            ("clean", "echo hi > out.txt", ExitClass::Ok),
            ("failed", "exit 1", ExitClass::WorkerCrash),
            ("cancelled", "sleep 5", ExitClass::Cancelled),
            ("timeout", "sleep 5", ExitClass::Timeout),
            ("question", "", ExitClass::NeedsInput),
            ("split", "exit 1", ExitClass::WorkerCrash),
            ("defect", "", ExitClass::ContractDefect),
        ] {
            let (r, base) = repo();
            let root = tempfile::tempdir().unwrap();
            let inp = input(
                claim(&r.path().to_string_lossy(), &base, &["out.txt"], &format!("echo '{secret}'")),
                1,
                root.path(),
            );
            let script = match mode {
                "split" => format!(
                    "printf '{secret}\\n{}\\n{}\\n'; {ending}",
                    &secret[..prefix.len()],
                    &secret[prefix.len()..]
                ),
                "defect" => format!("echo '{secret}'; echo hi > '{secret}'"),
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
            let result =
                crate::card_admit::run_attempt(Some(&h), &cfg, &Patterns::default(), &inp, &|| {
                    mode == "cancelled" && start.elapsed() > Duration::from_millis(300)
                })
                .unwrap();
            assert_eq!(result.exit_class, expected, "{mode}");
            if mode == "defect" {
                // Exercise the result boundary with raw output too: the process
                // collector may already have redacted the harness's stdout.
                let raw = defect_result(r.path(), &base, "pilot", &base, secret.clone())
                    .unwrap();
                assert_eq!(raw.output_tail, "[REDACTED]");
                assert_eq!(result.output_tail, "[REDACTED]");
            }
            let safe = |body: &str| {
                assert!(
                    excludes_secret_fragments(body, &secret, floor),
                    "{mode}: leaked secret fragment"
                );
                assert!(!body.contains(fragment), "{mode}: leaked short suffix");
            };
            if mode == "split" {
                assert_eq!(result.output_tail, "[REDACTED]");
            }
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
}
