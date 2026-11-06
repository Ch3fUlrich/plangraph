use super::*;
pub(crate) fn fixture() -> (tempfile::TempDir, RepoEntry, String, String) {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    command(root, &["init"], &[], None).unwrap();
    std::fs::write(root.join("é.txt"), ["gh", "p_", &"a".repeat(25)].concat()).unwrap();
    command(root, &["add", "."], &[], None).unwrap();
    command(root, &["commit", "-m", "base"], &[], None).unwrap();
    let base = String::from_utf8(command(root, &["rev-parse", "HEAD"], &[], None).unwrap())
        .unwrap()
        .trim()
        .to_owned();
    std::fs::write(root.join("safe"), "safe").unwrap();
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
fn inherited_blob_is_scanned() {
    let (_dir, repo, base, tip) = fixture();
    assert_eq!(findings(&repo, &base, &tip).unwrap().len(), 1);
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
        vec![SecretFinding {
            commit: tip,
            path: Some("é.txt".into())
        }]
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
