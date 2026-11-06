use super::*;
use crate::writer_secret::{findings, tests::fixture};
#[test]
fn rebuild_excludes_original_objects() {
    let (dir, repo, base, tip) = fixture();
    let clean = sanitize_history(&repo, &base, &tip).unwrap();
    assert_ne!(clean, tip);
    assert!(findings(&repo, &base, &clean).unwrap().is_empty());
    let tree = entries(dir.path(), &clean).unwrap();
    assert_eq!(tree.len(), 1);
    assert_eq!(tree[0].2, "safe");
    let history = command(
        dir.path(),
        &["rev-list", &format!("{base}..{clean}")],
        &[],
        None,
    )
    .unwrap();
    assert!(!String::from_utf8(history).unwrap().contains(&tip));
}
#[test]
fn invalid_revision_refused() {
    let (_dir, repo, base, _) = fixture();
    assert!(sanitize_history(&repo, &base, "HEAD").is_err());
}
#[test]
fn historical_message_and_blob_removed() {
    let (dir, repo, base, _) = fixture();
    let token = ["gh", "p_", &"b".repeat(25)].concat();
    std::fs::write(dir.path().join("historical"), &token).unwrap();
    command(dir.path(), &["add", "."], &[], None).unwrap();
    command(dir.path(), &["commit", "-m", &token], &[], None).unwrap();
    command(dir.path(), &["rm", "historical"], &[], None).unwrap();
    command(dir.path(), &["commit", "-m", "removed"], &[], None).unwrap();
    let tip = command(dir.path(), &["rev-parse", "HEAD"], &[], None).unwrap();
    let tip = std::str::from_utf8(&tip).unwrap().trim();
    assert!(findings(&repo, &base, tip).unwrap().len() >= 3);
    let clean = sanitize_history(&repo, &base, tip).unwrap();
    assert!(findings(&repo, &base, &clean).unwrap().is_empty());
}
