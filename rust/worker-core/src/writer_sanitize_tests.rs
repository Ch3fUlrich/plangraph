use super::*;
use crate::writer_secret::{findings, tests::fixture};
#[test]
fn sanitized_objects_no_original_secret() {
    let (dir, repo, base, tip) = fixture();
    let clean = sanitize_history(&repo, &base, &tip).unwrap().tip;
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
    let clean = sanitize_history(&repo, &base, tip).unwrap().tip;
    assert!(findings(&repo, &base, &clean).unwrap().is_empty());
}
#[test]
fn matching_message_placeholder_and_trailer() {
    let (dir, repo, base, tip) = fixture();
    let token = ["gh", "p_", &"c".repeat(25)].concat();
    command(
        dir.path(),
        &["commit", "--allow-empty", "-m", &token],
        &[],
        None,
    )
    .unwrap();
    let head = command(dir.path(), &["rev-parse", "HEAD"], &[], None).unwrap();
    let head = std::str::from_utf8(&head).unwrap().trim();
    let clean = sanitize_history(&repo, &base, head).unwrap();
    let safe = object(dir.path(), &clean.sha_map[&tip]).unwrap().1;
    assert_eq!(
        message(&safe).unwrap(),
        b"export\n\n\nSanitized: secret-gate\n"
    );
    let removed = object(dir.path(), &clean.tip).unwrap().1;
    assert_eq!(
        message(&removed).unwrap(),
        b"Message removed by secret gate\n\n\nSanitized: secret-gate\n"
    );
    assert!(clean.dropped_paths.contains("é.txt"));
}
#[test]
fn merge_parent_map_and_safe_messages() {
    let (dir, repo, base, tip) = fixture();
    command(dir.path(), &["checkout", "-b", "side", &base], &[], None).unwrap();
    command(
        dir.path(),
        &["commit", "--allow-empty", "-m", "side"],
        &[],
        None,
    )
    .unwrap();
    let side = command(dir.path(), &["rev-parse", "HEAD"], &[], None).unwrap();
    let side = std::str::from_utf8(&side).unwrap().trim();
    command(dir.path(), &["checkout", "--detach", &tip], &[], None).unwrap();
    command(
        dir.path(),
        &["merge", "--no-ff", "side", "-m", "merge"],
        &[],
        None,
    )
    .unwrap();
    let head = command(dir.path(), &["rev-parse", "HEAD"], &[], None).unwrap();
    let clean = sanitize_history(&repo, &base, std::str::from_utf8(&head).unwrap().trim()).unwrap();
    for (old, new) in &clean.sha_map {
        let parents = |sha: &str| {
            String::from_utf8(
                command(dir.path(), &["show", "-s", "--format=%P", sha], &[], None).unwrap(),
            )
            .unwrap()
            .split_whitespace()
            .map(str::to_owned)
            .collect::<Vec<_>>()
        };
        let expected: Vec<_> = parents(old)
            .into_iter()
            .map(|p| clean.sha_map.get(&p).cloned().unwrap_or(p))
            .collect();
        assert_eq!(parents(new), expected);
        let old_raw = object(dir.path(), old).unwrap().1;
        let new_raw = object(dir.path(), new).unwrap().1;
        let mut expected_message = message(&old_raw).unwrap().to_vec();
        expected_message.extend_from_slice(b"\n\nSanitized: secret-gate\n");
        assert_eq!(message(&new_raw).unwrap(), expected_message);
    }
    assert!(clean.sha_map.contains_key(side));
    assert_eq!(clean.sha_map.len(), 3);
    let reachable = command(
        dir.path(),
        &["rev-list", "--objects", &format!("{base}..{}", clean.tip)],
        &[],
        None,
    )
    .unwrap();
    let reachable = String::from_utf8(reachable).unwrap();
    for original in clean.sha_map.keys() {
        assert!(!reachable.contains(original));
    }
    assert!(findings(&repo, &base, &clean.tip).unwrap().is_empty());
}
#[test]
fn dirty_prerequisite_refused_even_when_tip_removes_secret() {
    let (dir, repo, base, tip) = fixture();
    command(dir.path(), &["rm", "é.txt"], &[], None).unwrap();
    command(dir.path(), &["commit", "-m", "remove"], &[], None).unwrap();
    let head = command(dir.path(), &["rev-parse", "HEAD"], &[], None).unwrap();
    assert!(sanitize_history(&repo, &tip, std::str::from_utf8(&head).unwrap().trim()).is_err());
    assert!(sanitize_history(&repo, &base, &tip).is_ok());
}

#[test]
fn receiver_rejects_ungated_bundle() {
    let (dir, mut repo, base, tip) = fixture();
    let bundle = dir.path().join("ungated.bundle");
    command(
        dir.path(),
        &["bundle", "create", bundle.to_str().unwrap(), "HEAD"],
        &[],
        None,
    )
    .unwrap();
    let receiver = tempfile::tempdir().unwrap();
    command(receiver.path(), &["init"], &[], None).unwrap();
    command(
        receiver.path(),
        &["fetch", bundle.to_str().unwrap(), "HEAD"],
        &[],
        None,
    )
    .unwrap();
    repo.root = receiver.path().to_str().unwrap().into();
    assert!(matches!(
        SecretGate.check(&repo, &base, &tip),
        Err(WriterError::SecretInHistory)
    ));
}
