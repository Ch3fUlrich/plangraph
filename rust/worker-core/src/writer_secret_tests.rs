use super::*;
#[test]
fn scan_bytes_high_byte_before_token() {
    let mut bytes = vec![0xd0, 0xba];
    bytes.extend(["gh", "p_", &"a".repeat(25)].concat().bytes());
    assert!(scan_bytes(&bytes));
}

#[test]
fn token_in_author_refused() {
    let (dir, repo, base, _) = fixture();
    let tree = command(
        dir.path(),
        &["rev-parse", &format!("{base}^{{tree}}")],
        &[],
        None,
    )
    .unwrap();
    let tree = std::str::from_utf8(&tree).unwrap().trim();
    let token = ["gh", "p_", &"a".repeat(25)].concat();
    for header in ["author", "committer"] {
        let raw = format!("tree {tree}\nparent {base}\nauthor Safe <gate@example.invalid> 0 +0000\ncommitter Safe <gate@example.invalid> 0 +0000\n\nsafe\n");
        let raw = raw.replace(&format!("{header} Safe"), &format!("{header} {token}"));
        let tip = command(
            dir.path(),
            &["hash-object", "-t", "commit", "-w", "--stdin"],
            raw.as_bytes(),
            None,
        )
        .unwrap();
        let tip = std::str::from_utf8(&tip).unwrap().trim();
        assert_eq!(
            findings(&repo, &base, tip).unwrap(),
            vec![SecretFinding {
                commit: tip.into(),
                path: None
            }]
        );
        assert!(matches!(
            SecretGate.check(&repo, &base, tip),
            Err(WriterError::SecretInHistory)
        ));
        let clean = crate::writer_sanitize::sanitize_history(&repo, &base, tip).unwrap();
        assert!(findings(&repo, &base, &clean.tip).unwrap().is_empty());
    }
}
pub(crate) fn fixture() -> (tempfile::TempDir, RepoEntry, String, String) {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    command(root, &["init"], &[], None).unwrap();
    std::fs::write(root.join("initial"), "safe").unwrap();
    command(root, &["add", "."], &[], None).unwrap();
    command(root, &["commit", "-m", "base"], &[], None).unwrap();
    let base = String::from_utf8(command(root, &["rev-parse", "HEAD"], &[], None).unwrap())
        .unwrap()
        .trim()
        .to_owned();
    std::fs::write(root.join("safe"), "safe").unwrap();
    command(root, &["rm", "initial"], &[], None).unwrap();
    std::fs::write(root.join("é.txt"), ["gh", "p_", &"a".repeat(25)].concat()).unwrap();
    command(root, &["add", "."], &[], None).unwrap();
    command(root, &["commit", "-m", "export"], &[], None).unwrap();
    let tip = String::from_utf8(command(root, &["rev-parse", "HEAD"], &[], None).unwrap())
        .unwrap()
        .trim()
        .to_owned();
    let repo = RepoEntry {
        root: root.to_str().unwrap().into(),
        worktrees_root: String::new(),
        remote: String::new(),
        python: None,
        import_roots: vec![],
        cache_env: Default::default(),
    };
    (dir, repo, base, tip)
}
#[test]
fn every_reachable_blob_scanned() {
    let (dir, repo, base, _) = fixture();
    let token = ["gh", "p_", &"z".repeat(25)].concat();
    std::fs::write(dir.path().join("second"), token).unwrap();
    command(dir.path(), &["add", "."], &[], None).unwrap();
    command(dir.path(), &["commit", "-m", "two blobs"], &[], None).unwrap();
    let head = command(dir.path(), &["rev-parse", "HEAD"], &[], None).unwrap();
    let found = findings(&repo, &base, std::str::from_utf8(&head).unwrap().trim()).unwrap();
    assert_eq!(found.len(), 3);
    assert!(found.iter().any(|f| f.path.as_deref() == Some("second")));
    assert_eq!(
        found
            .iter()
            .filter(|f| f.path.as_deref() == Some("é.txt"))
            .count(),
        2
    );
}
#[test]
fn utf8_path_and_introducing_commit() {
    let (dir, repo, base, tip) = fixture();
    command(
        dir.path(),
        &["commit", "--allow-empty", "-m", "later"],
        &[],
        None,
    )
    .unwrap();
    let later =
        String::from_utf8(command(dir.path(), &["rev-parse", "HEAD"], &[], None).unwrap()).unwrap();
    assert_eq!(
        findings(&repo, &base, later.trim()).unwrap(),
        vec![
            SecretFinding {
                commit: tip,
                path: Some("é.txt".into())
            },
            SecretFinding {
                commit: later.trim().into(),
                path: Some("é.txt".into())
            }
        ]
    );
}
#[test]
fn continued_header_keeps_real_message() {
    let token = ["gh", "p_", &"a".repeat(25)].concat();
    let raw = format!(
        "tree {}\nmergetag object {}\n type commit\n \n embedded\n\n{token}",
        "a".repeat(40),
        "b".repeat(40)
    );
    assert_eq!(message(raw.as_bytes()).unwrap(), token.as_bytes());
    assert!(scan_bytes(message(raw.as_bytes()).unwrap()));
}
#[test]
fn binary_and_multiline_secret_detected() {
    let mut bytes = vec![255, 0];
    bytes.extend(["gh", "p_", &"a".repeat(25)].concat().bytes());
    assert!(scan_bytes(&bytes));
    let pem = [
        "-----BEGIN ",
        "PRIVATE KEY-----\nbody\n-----END ",
        "PRIVATE KEY-----",
    ]
    .concat();
    assert!(scan_bytes(pem.as_bytes()));
    assert!(!scan_bytes(b"safe text"));
}
#[test]
fn reintroduced_secret_reports_each_commit_path_without_bytes() {
    let (dir, repo, base, tip) = fixture();
    let bytes = std::fs::read(dir.path().join("é.txt")).unwrap();
    command(dir.path(), &["rm", "é.txt"], &[], None).unwrap();
    command(dir.path(), &["commit", "-m", "remove"], &[], None).unwrap();
    std::fs::write(dir.path().join("é.txt"), &bytes).unwrap();
    command(dir.path(), &["add", "."], &[], None).unwrap();
    command(dir.path(), &["commit", "-m", "reintroduce"], &[], None).unwrap();
    let head = command(dir.path(), &["rev-parse", "HEAD"], &[], None).unwrap();
    let head = std::str::from_utf8(&head).unwrap().trim();
    let found = findings(&repo, &base, head).unwrap();
    assert_eq!(
        found,
        vec![
            SecretFinding {
                commit: tip,
                path: Some("é.txt".into())
            },
            SecretFinding {
                commit: head.into(),
                path: Some("é.txt".into())
            }
        ]
    );
    assert!(!format!("{found:?}").contains(std::str::from_utf8(&bytes).unwrap()));
    assert!(crate::writer_result::ContentGate::check(&SecretGate, &repo, &base, head).is_err());
}

#[test]
fn committed_added_deleted_and_message_secrets_refuse_result() {
    let (dir, repo, base, tip) = fixture();
    command(dir.path(), &["rm", "é.txt"], &[], None).unwrap();
    let token = ["gh", "p_", &"d".repeat(25)].concat();
    command(dir.path(), &["commit", "-m", &token], &[], None).unwrap();
    let head = command(dir.path(), &["rev-parse", "HEAD"], &[], None).unwrap();
    let head = std::str::from_utf8(&head).unwrap().trim();
    let found = findings(&repo, &base, head).unwrap();
    assert_eq!(
        found,
        vec![
            SecretFinding {
                commit: tip,
                path: Some("é.txt".into())
            },
            SecretFinding {
                commit: head.into(),
                path: None
            },
        ]
    );
    assert!(!format!("{found:?}").contains(&token));
    assert!(matches!(
        SecretGate.check(&repo, &base, head),
        Err(WriterError::SecretInHistory)
    ));
}

#[test]
fn no_serial_bundle_scan() {
    let (dir, repo, base, tip) = fixture();
    let clean = crate::writer_sanitize::sanitize_history(&repo, &base, &tip).unwrap();
    // A ref name is transport metadata, not reachable file or message content.
    let token = ["gh", "p_", &"e".repeat(25)].concat();
    let reference = format!("refs/heads/{token}");
    command(
        dir.path(),
        &["update-ref", &reference, &clean.tip],
        &[],
        None,
    )
    .unwrap();
    let bundle = dir.path().join("transport.bundle");
    command(
        dir.path(),
        &["bundle", "create", bundle.to_str().unwrap(), &reference],
        &[],
        None,
    )
    .unwrap();
    assert!(scan_bytes(&std::fs::read(bundle).unwrap()));
    assert!(SecretGate.check(&repo, &base, &clean.tip).is_ok());
}

pub(crate) fn nested_fixture() -> (tempfile::TempDir, RepoEntry, String, String) {
    let (dir, repo, base, _) = fixture();
    std::fs::create_dir_all(dir.path().join("nested/dir")).unwrap();
    std::fs::write(
        dir.path().join("nested/dir/secret"),
        ["gh", "p_", &"n".repeat(25)].concat(),
    )
    .unwrap();
    std::fs::write(dir.path().join("nested/dir/safe"), "keep me").unwrap();
    command(dir.path(), &["add", "."], &[], None).unwrap();
    command(dir.path(), &["commit", "-m", "nested"], &[], None).unwrap();
    let tip =
        String::from_utf8(command(dir.path(), &["rev-parse", "HEAD"], &[], None).unwrap()).unwrap();
    (dir, repo, base, tip.trim().into())
}

#[test]
fn nested_secret_path_detected() {
    let (_dir, repo, base, tip) = nested_fixture();
    let found = findings(&repo, &base, &tip).unwrap();
    assert!(found
        .iter()
        .any(|f| f.commit == tip && f.path.as_deref() == Some("nested/dir/secret")));
}

#[test]
fn utf16_tokens_detected() {
    let token = ["gh", "p_", &"u".repeat(25)].concat();
    for be in [false, true] {
        let bytes: Vec<_> = token
            .encode_utf16()
            .flat_map(|c| if be { c.to_be_bytes() } else { c.to_le_bytes() })
            .collect();
        assert!(scan_bytes(&bytes));
    }
}

#[cfg(unix)]
#[test]
fn non_utf8_path_scanned() {
    use std::os::unix::ffi::OsStrExt;
    let (dir, repo, base, _) = fixture();
    std::fs::write(
        dir.path().join(std::ffi::OsStr::from_bytes(b"raw-\xff")),
        ["gh", "p_", &"v".repeat(25)].concat(),
    )
    .unwrap();
    command(dir.path(), &["add", "."], &[], None).unwrap();
    command(dir.path(), &["commit", "-m", "binary name"], &[], None).unwrap();
    let head = command(dir.path(), &["rev-parse", "HEAD"], &[], None).unwrap();
    let found = findings(&repo, &base, std::str::from_utf8(&head).unwrap().trim()).unwrap();
    assert!(found.iter().any(|f| f
        .path
        .as_deref()
        .is_some_and(|p| p.starts_with("<non-UTF-8 path:"))));
}
