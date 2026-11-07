use super::*;

#[test]
fn startup_loads_env_policy_and_unloaded_config_refuses() {
    // A subprocess isolates the real environment loader from parallel tests.
    if std::env::var_os("PG_PILOT_ALLOWLIST_CHILD").is_some() {
        let loaded = Config::default().with_card_allowlist_env().unwrap();
        assert!(allow(&loaded, "startup-listed").is_ok());
        assert!(allow(&loaded, "unlisted").is_err());
        return;
    }
    let cfg = Config::default();
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("cards");
    std::fs::write(&path, "startup-listed\n").unwrap();
    assert!(
        std::process::Command::new(std::env::current_exe().unwrap())
            .args([
                "--exact",
                "card_admit::tests::startup_loads_env_policy_and_unloaded_config_refuses"
            ])
            .env("PG_PILOT_ALLOWLIST_CHILD", "1")
            .env("PG_CARD_ALLOWLIST", &path)
            .status()
            .unwrap()
            .success()
    );
    let loaded = Config::default().with_card_allowlist(Some(&path)).unwrap();
    assert!(allow(&loaded, "startup-listed").is_ok());
    assert!(allow(&loaded, "unlisted").is_err());
    assert!(
        Config::default()
            .with_card_allowlist(Some(dir.path()))
            .is_err()
    );
    assert_failure(
        allow(&cfg, "unloaded").unwrap_err(),
        Some("card_not_allowlisted"),
        true,
    );
    let reason = enter(&cfg, "unloaded")
        .err()
        .expect("unloaded policy admitted");
    assert!(matches!(reason, AdmissionReason::NotAllowlisted(_)));
    assert_failure(
        attempt_error(reason.into()),
        Some("card_not_allowlisted"),
        true,
    );
}

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
    let cfg = Config::allow_all_for_tests();
    let key = "pilot-owner-test";
    let first = enter(&cfg, key).unwrap();
    assert!(
        matches!(enter(&cfg, key), Err(AdmissionReason::Overlap(ref refused)) if refused == key)
    );
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
        ..Config::allow_all_for_tests()
    };
    assert!(allow(&cfg, "listed").is_ok());
    assert!(allow(&cfg, "unlisted").is_err());
    assert!(claimed(None, "listed").is_err());
}
