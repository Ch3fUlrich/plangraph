#[test]
fn allowlist_comments_and_keys() {
    let dir = tempfile::tempdir().unwrap();
    let file = dir.path().join("cards");
    std::fs::write(&file, "\u{feff}c0\n# lane A\n\n c1 \n").unwrap();
    let cfg = Config::default().with_card_allowlist(Some(&file)).unwrap();
    assert!(cfg.card_allowed("c1"));
    assert!(cfg.card_allowed("c0"));
    assert!(!cfg.card_allowed("c2"));
    assert!(!Config::default().card_allowed("c2"));
    assert!(Config::allow_all_for_tests().card_allowed("c2"));
}

#[test]
fn empty_allowlist_fails_closed() {
    let dir = tempfile::tempdir().unwrap();
    let file = dir.path().join("cards");
    std::fs::write(&file, "# nothing\n\n").unwrap();
    assert!(Config::default().with_card_allowlist(Some(&file)).is_err());
}

#[test]
fn unreadable_allowlist_fails_closed() {
    assert!(
        Config::default()
            .with_card_allowlist(Some(std::path::Path::new("/nonexistent/pilot/cards")))
            .is_err()
    );
}

#[test]
fn directory_allowlist_reports_path_and_kind() {
    let dir = tempfile::tempdir().unwrap();
    let expected = std::fs::read_to_string(dir.path()).unwrap_err();
    let error = Config::default().with_card_allowlist(Some(dir.path())).unwrap_err();
    assert_eq!(error.kind(), expected.kind());
    assert!(error.to_string().contains(&dir.path().display().to_string()));
    assert!(error.to_string().contains(&format!("{:?}", expected.kind())));
}

#[cfg(unix)]
#[test]
fn permission_denied_allowlist_fails_without_contents() {
    use std::os::unix::fs::PermissionsExt;
    let dir = tempfile::tempdir().unwrap();
    let file = dir.path().join("cards");
    let contents = ["private", "card"].join("-");
    std::fs::write(&file, &contents).unwrap();
    std::fs::set_permissions(&file, std::fs::Permissions::from_mode(0o0)).unwrap();
    let read = std::fs::read_to_string(&file);
    let result = Config::default().with_card_allowlist(Some(&file));
    std::fs::set_permissions(&file, std::fs::Permissions::from_mode(0o600)).unwrap();
    // Privileged runners can read mode-000 files; the directory test still
    // exercises a real read failure there.
    if let Err(expected) = read {
        let error = result.unwrap_err();
        assert_eq!(error.kind(), expected.kind());
        assert_eq!(error.kind(), std::io::ErrorKind::PermissionDenied);
        assert!(error.to_string().contains(&file.display().to_string()));
        assert!(!error.to_string().contains(&contents));
    } else {
        assert_eq!(read.unwrap(), contents);
        assert!(result.unwrap().card_allowed(&contents));
    }
}
#[test]
fn startup_environment_source_is_loaded_once_and_explicit_policy_wins() {
    let dir = tempfile::tempdir().unwrap();
    let file = dir.path().join("cards");
    std::fs::write(&file, "startup-card\n").unwrap();
    let cfg = Config::default().with_card_allowlist_source(|| Some(file.into_os_string())).unwrap();
    assert!(cfg.card_allowed("startup-card"));
    assert!(!cfg.card_allowed("unlisted"));
    let cfg = cfg.with_card_allowlist_source(|| panic!("explicit policy must win")).unwrap();
    assert!(cfg.card_allowed("startup-card"));
    assert!(!Config::default().with_card_allowlist_source(|| None).unwrap().card_allowed("startup-card"));
    assert!(Config::default().with_card_allowlist_source(|| Some(dir.path().into())).is_err());
}
