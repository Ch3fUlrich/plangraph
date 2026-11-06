use super::*;
use crate::writer_secret::tests::fixture;
#[test]
fn rejects_traversal_and_non_nul_paths() {
    let (_dir, repo, base, tip) = fixture();
    for paths in [
        b"../escape\0".as_slice(),
        b"safe",
        b"/absolute\0",
        b"--help\0",
        b"a\\b\0",
    ] {
        assert!(export_salvage(&repo, &base, &tip, "run", paths).is_err());
    }
}
#[test]
fn rejects_argument_injection() {
    let (_dir, repo, base, tip) = fixture();
    assert!(export_salvage(&repo, "--help", &tip, "run", b"").is_err());
    assert!(export_salvage(&repo, &base, &tip, "../run", b"").is_err());
}
#[test]
fn bundle_roundtrip_and_ref_cleanup() {
    let (dir, repo, base, tip) = fixture();
    let bytes = export_salvage(&repo, &base, &tip, "roundtrip", b"").unwrap();
    let bundle = dir.path().join("result.bundle");
    std::fs::write(&bundle, bytes).unwrap();
    command(
        dir.path(),
        &["bundle", "verify", bundle.to_str().unwrap()],
        &[],
        None,
    )
    .unwrap();
    command(
        dir.path(),
        &[
            "fetch",
            bundle.to_str().unwrap(),
            "refs/plangraph-salvage/roundtrip",
        ],
        &[],
        None,
    )
    .unwrap();
    let clean = command(dir.path(), &["rev-parse", "FETCH_HEAD"], &[], None).unwrap();
    assert!(crate::writer_secret::findings(
        &repo,
        &base,
        std::str::from_utf8(&clean).unwrap().trim()
    )
    .unwrap()
    .is_empty());
    assert!(command(
        dir.path(),
        &["show-ref", "--verify", "refs/plangraph-salvage/roundtrip"],
        &[],
        None
    )
    .is_err());
}
#[test]
fn existing_ref_is_not_overwritten() {
    let (dir, repo, base, tip) = fixture();
    command(
        dir.path(),
        &["update-ref", "refs/plangraph-salvage/busy", &tip],
        &[],
        None,
    )
    .unwrap();
    assert!(export_salvage(&repo, &base, &tip, "busy", b"").is_err());
    assert_eq!(
        command(
            dir.path(),
            &["rev-parse", "refs/plangraph-salvage/busy"],
            &[],
            None
        )
        .unwrap(),
        format!("{tip}\n").as_bytes()
    );
}
#[test]
fn preserves_index_and_owned_tree() {
    let (dir, repo, base, tip) = fixture();
    std::fs::write(dir.path().join("safe"), "staged").unwrap();
    command(dir.path(), &["add", "safe"], &[], None).unwrap();
    std::fs::write(dir.path().join("safe"), "unstaged").unwrap();
    let index = std::fs::read(dir.path().join(".git/index")).unwrap();
    let bundle = export_salvage(&repo, &base, &tip, "run", b"safe\0").unwrap();
    assert!(bundle.starts_with(b"# v2 git bundle"));
    assert_eq!(std::fs::read(dir.path().join(".git/index")).unwrap(), index);
    assert_eq!(std::fs::read(dir.path().join("safe")).unwrap(), b"unstaged");
    assert_eq!(
        command(dir.path(), &["rev-parse", "HEAD"], &[], None).unwrap(),
        format!("{tip}\n").as_bytes()
    );
}
