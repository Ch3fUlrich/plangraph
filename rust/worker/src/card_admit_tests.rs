use super::*;

#[test]
fn only_admission_is_non_retryable() {
    assert_failure(
        attempt_error(AdmissionReason::NotAllowlisted("s2-mapping".into()).into()),
        Some("card_not_allowlisted"),
        true,
    );
    let io = crate::attempt::AttemptError::Io(std::io::Error::from(
        std::io::ErrorKind::PermissionDenied,
    ));
    assert_failure(attempt_error(io.into()), None, false);
}

pub(crate) fn assert_failure(error: ActivityError, kind: Option<&str>, terminal: bool) {
    let ActivityError::Application(failure) = error else {
        panic!("expected application failure")
    };
    assert_eq!(failure.type_name(), kind);
    assert_eq!(failure.is_non_retryable(), terminal);
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
