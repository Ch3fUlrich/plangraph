use super::*;

fn random_body() -> String {
    use std::io::Read;
    let mut bytes = [0u8; 16];
    std::fs::File::open("/dev/urandom").unwrap().read_exact(&mut bytes).unwrap();
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

fn excludes_secret_fragments(body: &str, secret: &str, floor: usize) -> bool {
    assert!(floor <= secret.len(), "family floor exceeds fixture length");
    !body.contains(secret)
        && (1..=secret.len() - floor).all(|start| !body.contains(&secret[start..]))
        && (floor..secret.len()).all(|end| !body.contains(&secret[..end]))
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
}
/// How long either side of the owner test waits for the other before it panics
/// with the reason, instead of hanging the whole test binary.
const PILOT_GATE_WAIT: Duration = Duration::from_secs(30);

/// A two-party hand-off with a deadline: `Barrier::wait` blocks forever when the
/// other side already ended (for example the attempt failed before it reached
/// the harness), this one panics with the stage that never arrived.
struct Gate {
    entered: std::sync::mpsc::Sender<()>,
    release: std::sync::Mutex<std::sync::mpsc::Receiver<()>>,
}

#[test]
fn pilot_activity_owner_and_empty_claim() {
    struct Held(Gate, Fake);
    impl Harness for Held {
        fn name(&self) -> &str {
            "held"
        }
        fn command(&self, run: &HarnessRun) -> Command {
            self.0.entered.send(()).expect("test side hung up before the harness ran");
            self.0
                .release
                .lock()
                .unwrap()
                .recv_timeout(PILOT_GATE_WAIT)
                .expect("the second attempt never released the held harness");
            self.1.command(run)
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
    let (entered_tx, entered_rx) = std::sync::mpsc::channel();
    let (release_tx, release_rx) = std::sync::mpsc::channel();
    let held = Arc::new(Held(
        Gate {
            entered: entered_tx,
            release: std::sync::Mutex::new(release_rx),
        },
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
    // The first attempt must reach its harness. If it ended before, say how: on a
    // host without PG_JAIL=off and PG_TEST_ALLOW_UNJAILED=1 it ends at once with
    // the jail refusal, and waiting for it would only hang.
    let deadline = std::time::Instant::now() + PILOT_GATE_WAIT;
    while entered_rx.recv_timeout(Duration::from_millis(50)).is_err() {
        if first.is_finished() {
            panic!(
                "the first attempt ended before its harness ran (a worker test run needs \
                 PG_JAIL=off and PG_TEST_ALLOW_UNJAILED=1): {:?}",
                first.join()
            );
        }
        assert!(
            std::time::Instant::now() < deadline,
            "the first attempt did not reach its harness within {PILOT_GATE_WAIT:?}"
        );
    }
    let never = Fake::new("exit 99");
    let second =
        crate::card_admit::run_attempt(Some(&never), &cfg, &Patterns::default(), &inp, &|| false);
    release_tx.send(()).unwrap();
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
            // The outputs the workflow itself builds for this result: the keeper
            // report, and the tail its park packets carry.
            let (report, entry, tail) =
                crate::workflow::attempt_outputs(&inp.claim.card, 1, &inp.leg, &result);
            let packet = crate::workflow::packet_from_last(&inp.claim.card, &[entry], &tail);
            assert_eq!(report.check_tail, result.check_tail, "{mode}: report tail source");
            assert_eq!(packet.check_tail, result.check_tail, "{mode}: park tail source");
            safe(&serde_json::to_string(&report).unwrap());
            safe(&packet.to_json());
        }
    }
}
