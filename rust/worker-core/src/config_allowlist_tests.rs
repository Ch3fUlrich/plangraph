#[test]
fn allowlist_comments_and_keys() {
    let dir = tempfile::tempdir().unwrap();
    let file = dir.path().join("cards");
    std::fs::write(&file, "# lane A\n\n c1 \n").unwrap();
    let cfg = Config::default().with_card_allowlist(Some(&file)).unwrap();
    assert!(cfg.card_allowed("c1"));
    assert!(!cfg.card_allowed("c2"));
    assert!(Config::default().card_allowed("c2"));
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
