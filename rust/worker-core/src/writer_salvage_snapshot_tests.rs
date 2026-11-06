use super::tests::{export_salvage, fixture, imported};
use super::*;
use crate::writer_secret::object;

#[test]
fn dirty_untracked_snapshot_ignored_excluded() {
    let (_dir, repo, base, _) = fixture();
    let root = Path::new(&repo.root);
    std::fs::write(root.join(".gitignore"), "ignored\n").unwrap();
    std::fs::write(root.join("ignored"), "not exported").unwrap();
    std::fs::write(root.join("new\nfile"), "new bytes").unwrap();
    std::fs::write(root.join("safe"), "dirty bytes").unwrap();
    let token = ["gh", "p_", &"s".repeat(25)].concat();
    std::fs::write(root.join("bad"), &token).unwrap();
    let bytes = export_salvage(&repo, &base, "run").unwrap();
    let (dest, receiver, head) = imported(&repo, &bytes);
    let names = entries(dest.path(), &head).unwrap();
    assert!(!names.iter().any(|e| e.2 == b"ignored" || e.2 == b"bad"));
    for (name, expected) in [("safe", "dirty bytes"), ("new\nfile", "new bytes")] {
        let blob = &names.iter().find(|e| e.2 == name.as_bytes()).unwrap().1;
        assert_eq!(object(dest.path(), blob).unwrap().1, expected.as_bytes());
    }
    assert!(crate::writer_secret::findings(&receiver, &base, &head)
        .unwrap()
        .is_empty());
    let hash = command(root, &["hash-object", "--stdin"], token.as_bytes(), None).unwrap();
    assert!(object(root, std::str::from_utf8(&hash).unwrap().trim()).is_err());
}

#[test]
fn failed_writer_returns_blob_without_host_access() {
    committed_secret_roundtrip(true);
}

#[test]
fn unchanged_committed_secret_reaches_history_sanitization() {
    committed_secret_roundtrip(false);
}

fn committed_secret_roundtrip(dirty: bool) {
    let (_dir, repo, base, _) = fixture();
    let root = Path::new(&repo.root);
    std::fs::write(
        root.join("committed-secret"),
        ["gh", "p_", &"t".repeat(25)].concat(),
    )
    .unwrap();
    command(root, &["add", "."], &[], None).unwrap();
    command(root, &["commit", "-m", "failed without trailer"], &[], None).unwrap();
    let old = command(root, &["rev-parse", "HEAD"], &[], None).unwrap();
    if dirty {
        std::fs::write(
            root.join("committed-secret"),
            ["gh", "p_", &"u".repeat(25)].concat(),
        )
        .unwrap();
    } else {
        let tip = std::str::from_utf8(&old).unwrap().trim();
        let (snapshot, dropped) = super::snapshot(root, &base, tip).unwrap();
        assert!(dropped.is_empty());
        assert!(entries(root, &snapshot)
            .unwrap()
            .iter()
            .any(|e| e.2 == b"committed-secret"));
    }
    let bytes = export_salvage(&repo, &base, "run").unwrap();
    let (dest, receiver, head) = imported(&repo, &bytes);
    assert!(crate::writer_secret::findings(&receiver, &base, &head)
        .unwrap()
        .is_empty());
    let objects = command(
        dest.path(),
        &["rev-list", "--objects", &head, "--not", &base],
        &[],
        None,
    )
    .unwrap();
    assert!(!String::from_utf8(objects)
        .unwrap()
        .contains(std::str::from_utf8(&old).unwrap().trim()));
    assert!(!entries(dest.path(), &head)
        .unwrap()
        .iter()
        .any(|e| e.2 == b"committed-secret"));
}

#[cfg(unix)]
#[test]
fn unsafe_symlink_and_submodule_refused() {
    let (_dir, repo, base, _) = fixture();
    let root = Path::new(&repo.root);
    std::os::unix::fs::symlink("safe", root.join("link")).unwrap();
    assert!(export_salvage(&repo, &base, "run").is_err());
    std::fs::remove_file(root.join("link")).unwrap();
    command(
        root,
        &[
            "update-index",
            "--add",
            "--cacheinfo",
            &format!("160000,{base},sub"),
        ],
        &[],
        None,
    )
    .unwrap();
    command(root, &["commit", "-m", "submodule"], &[], None).unwrap();
    std::fs::create_dir(root.join("sub")).unwrap();
    std::fs::write(root.join("sub/file"), "dirty").unwrap();
    assert!(export_salvage(&repo, &base, "run").is_err());
}

#[test]
fn result_never_becomes_salvage_publication() {
    let (_dir, repo, base, _) = fixture();
    let root = Path::new(&repo.root);
    command(
        root,
        &["commit", "--allow-empty", "-m", "no trailer"],
        &[],
        None,
    )
    .unwrap();
    let ws = Workspace {
        repo_id: "test".into(),
        mode: Mode::Writer,
        base_sha: Some(base.clone()),
        revision_sha: Some(base.clone()),
        branch: Some("lane/test".into()),
    };
    let mut registry = repo.clone();
    registry.root = repo.remote.clone();
    assert!(crate::writer_result::check_result(&registry, root, &ws).is_err());
    let refs_before = command(root, &["show-ref"], &[], None).unwrap();
    export_salvage(&repo, &base, "run").unwrap();
    assert_eq!(
        command(root, &["show-ref"], &[], None).unwrap(),
        refs_before
    );
    assert!(crate::writer_result::check_result(&registry, root, &ws).is_err());
}

#[test]
fn gate_before_any_snapshot_object() {
    let (_dir, repo, base, _) = fixture();
    let root = Path::new(&repo.root);
    let safe = b"safe candidate not yet stored";
    std::fs::write(root.join("a-safe"), safe).unwrap();
    #[cfg(unix)]
    std::os::unix::fs::symlink("safe", root.join("z-unsafe")).unwrap();
    #[cfg(not(unix))]
    {
        std::fs::remove_file(root.join("safe")).unwrap();
        std::fs::create_dir(root.join("safe")).unwrap();
    }
    assert!(export_salvage(&repo, &base, "run").is_err());
    {
        let sha = command(root, &["hash-object", "--stdin"], safe, None).unwrap();
        assert!(object(root, std::str::from_utf8(&sha).unwrap().trim()).is_err());
    }
}

#[test]
fn unchanged_gitlink_preserved() {
    let (_dir, repo, base, _) = fixture();
    let root = Path::new(&repo.root);
    let gitlink_sha = base.clone();
    command(
        root,
        &[
            "update-index",
            "--add",
            "--cacheinfo",
            &format!("160000,{base},sub"),
        ],
        &[],
        None,
    )
    .unwrap();
    command(root, &["commit", "-m", "base gitlink"], &[], None).unwrap();
    std::fs::create_dir(root.join("sub")).unwrap();
    let base =
        String::from_utf8(command(root, &["rev-parse", "HEAD"], &[], None).unwrap()).unwrap();
    std::fs::write(root.join("safe"), "dirty").unwrap();
    command(
        Path::new(&repo.remote),
        &["update-ref", "refs/heads/clean-root", base.trim()],
        &[],
        None,
    )
    .unwrap();
    let bytes = export_salvage(&repo, base.trim(), "run").unwrap();
    let (dest, _, head) = imported(&repo, &bytes);
    let names = entries(dest.path(), &head).unwrap();
    let gitlink = names.iter().find(|e| e.2 == b"sub").unwrap();
    assert_eq!(gitlink.0, "160000");
    assert_eq!(gitlink.1, gitlink_sha);
}

#[test]
fn mode_lookup_is_cached_before_candidate_loop() {
    let source = include_str!("writer_salvage.rs");
    let scan = source.split("fn snapshot(").nth(1).unwrap();
    let lookup = scan.find("entries(root, tip)?").unwrap();
    assert_eq!(scan.matches("entries(root, tip)?").count(), 1);
    assert!(lookup < scan.find("for name in listed.split").unwrap());
}

#[cfg(unix)]
#[test]
fn dirty_executable_modes_preserved() {
    use std::os::unix::fs::PermissionsExt;
    let (_dir, repo, base, _) = fixture();
    let root = Path::new(&repo.root);
    std::fs::write(root.join("new"), "executable").unwrap();
    for name in ["safe", "new"] {
        std::fs::set_permissions(root.join(name), std::fs::Permissions::from_mode(0o755)).unwrap();
    }
    let bytes = export_salvage(&repo, &base, "run").unwrap();
    let (dest, _, head) = imported(&repo, &bytes);
    let names = entries(dest.path(), &head).unwrap();
    for name in [b"safe".as_slice(), b"new".as_slice()] {
        assert_eq!(names.iter().find(|e| e.2 == name).unwrap().0, "100755");
    }
}
