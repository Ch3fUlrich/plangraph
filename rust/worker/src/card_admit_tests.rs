use super::*;

#[derive(Default)]
pub(crate) struct CountingHarness(pub std::sync::atomic::AtomicUsize);
impl crate::harness::Harness for CountingHarness {
    fn name(&self) -> &str {
        "fake"
    }
    fn command(&self, _: &crate::harness::HarnessRun) -> std::process::Command {
        self.0.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
        std::process::Command::new("true")
    }
}

#[test]
fn only_admission_is_non_retryable() {
    let refused = format!(
        "{:?}",
        attempt_error(EntryError::Admission("s2-mapping".into()))
    );
    assert!(refused.contains("card_admission_refused") && refused.contains("non_retryable: true"));
    let io = crate::attempt::AttemptError::Io(std::io::Error::from(
        std::io::ErrorKind::PermissionDenied,
    ));
    let retryable = format!("{:?}", attempt_error(io.into()));
    assert!(!retryable.contains("card_admission_refused"));
    assert!(retryable.contains("non_retryable: false"));
}

#[test]
fn concurrent_key_and_release_on_unwind() {
    let cfg = Config::default();
    let key = "pilot-owner-test";
    let first = enter(&cfg, key).unwrap();
    assert!(enter(&cfg, key).err().unwrap().to_string().contains(key));
    drop(first);
    assert!(
        std::panic::catch_unwind(|| {
            let _owner = enter(&cfg, key).unwrap();
            panic!("test unwind");
        })
        .is_err()
    );
    assert!(enter(&cfg, key).is_ok());
}

#[test]
fn admission_and_no_claim_are_refusals() {
    let cfg = Config {
        card_allowlist: Some(["listed".to_owned()].into()),
        ..Config::default()
    };
    assert!(allow(&cfg, "listed").is_ok());
    assert!(allow(&cfg, "unlisted").is_err());
    assert!(claimed(None, "listed").is_err());
}
