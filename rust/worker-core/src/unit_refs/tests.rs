//! The unit-file contract: protection over-approximates, the report names only exact paths.
use super::*;

fn refs_of(text: &str) -> Vec<UnitRef> {
    let tmp = tempfile::tempdir().unwrap();
    let path = tmp.path().join("t.service");
    fs::write(path, text).unwrap();
    unit_file_refs(tmp.path(), Path::new("/h")).unwrap()
}
fn protected(refs: &[UnitRef], path: &str) -> bool {
    refs.iter().any(|r| r.path == Path::new(path))
}
fn exact(refs: &[UnitRef], path: &str) -> bool {
    refs.iter().any(|r| r.exact && r.path == Path::new(path))
}

/// One line per shape the reviews raised, with the worktree it names: each must protect it.
#[test]
fn protection_reads_every_piece_of_every_word() {
    let cases = [
        ("ExecStart=/bin/sh -c 'cat <\"/wt/app-worktrees/p1/config\"'", "p1"),
        ("ExecStart=/usr/bin/nice -n5 sh -c 'ROOT=/wt/app-worktrees/p2;true'", "p2"),
        ("ExecStart=/bin/sh -c \"/bin/true;/wt/app-worktrees/p3/run\"", "p3"),
        ("ExecStart=/usr/bin/env -- FOO=1 sh -c 'exec /wt/app-worktrees/p4/run'", "p4"),
        ("ExecStart=/usr/bin/env -S 'sh -c \"exec /wt/app-worktrees/p5/run\"'", "p5"),
        ("ExecStart=/bin/bash -O extglob -c 'exec /wt/app-worktrees/p6/run'", "p6"),
        ("ExecStart=/bin/sh -c '/wt/app-worktrees/p7/run # comment'", "p7"),
        ("ExecStart=/bin/sh -c '(/wt/app-worktrees/p8/run)&&/wt/app-worktrees/p9/run|cat'", "p9"),
        ("ExecStart=:/bin/sh -c 'exec /wt/app-worktrees/p10/run'", "p10"),
        ("ExecStart = /bin/sh -ce 'exec /wt/app-worktrees/p11/run'", "p11"),
        ("Environment=\"PATH=/a/bin:/wt/app-worktrees/p12/bin\"", "p12"),
        ("ExecStart=\"/wt/app-worktrees/my p13/bin/run\" --x", "my p13"),
        ("ExecStart=/bin/sh -c 'exec /wt/app-worktrees/p14/run' sh -ce \"/wt/app-worktrees/p15/c\"", "p15"),
    ];
    for (line, name) in cases {
        let refs = refs_of(&format!("[Service]\n{line}\n"));
        let tree = PathBuf::from("/wt/app-worktrees").join(name);
        assert_eq!(
            unit_names(&refs, &tree, &tree),
            ["t.service"],
            "{line}: {refs:?}"
        );
    }
}

#[test]
fn the_report_names_only_whole_words_and_path_valued_fields() {
    let refs = refs_of(
        "[Service]\nExecStart=/wt/app-worktrees/w1/bin/run --opt /wt/app-worktrees/w2/x\n\
         WorkingDirectory=\"/wt/app-worktrees/w3;blue dir\"\n\
         ExecStart=/bin/sh -c 'exec /wt/app-worktrees/s1/run'\n\
         ExecStart=\"/wt/app-worktrees/w4;blue/run\"\n\
         ExecStart=\"/wt/app-worktrees/w5; blue/run\"\n\
         ExecStart=\"/wt/app-worktrees/my w6/run\"\n\
         Environment=A=/wt/app-worktrees/w7/x\n",
    );
    for path in [
        "/wt/app-worktrees/w1/bin/run",
        "/wt/app-worktrees/w2/x",
        "/wt/app-worktrees/w3;blue dir",
    ] {
        assert!(exact(&refs, path), "{path}: {refs:?}");
    }
    // Shell text, separators, spaces outside a path field and assignments only protect.
    for path in [
        "/wt/app-worktrees/s1/run",
        "/wt/app-worktrees/w4;blue/run",
        "/wt/app-worktrees/w5; blue/run",
        "/wt/app-worktrees/my w6/run",
        "/wt/app-worktrees/w7/x",
    ] {
        assert!(!exact(&refs, path), "{path}: {refs:?}");
    }
    assert!(protected(&refs, "/wt/app-worktrees/s1/run"));
    assert!(protected(&refs, "/wt/app-worktrees/w7/x"));
}

#[test]
fn a_guess_never_reports_a_missing_worktree() {
    let tmp = tempfile::tempdir().unwrap();
    let trees = tmp.path().join("app-worktrees");
    fs::create_dir_all(trees.join("alive")).unwrap();
    let unit = |name: &str, path: PathBuf, exact: bool| UnitRef {
        unit: name.into(),
        path,
        exact,
    };
    let refs = vec![
        unit("a.service", trees.join("alive/run"), true),
        unit("b.service", trees.join("gone/run"), true),
        unit("c.service", trees.join("guess/run"), false),
        unit("d.service", tmp.path().join("main/run"), true),
    ];
    assert_eq!(missing_worktree_refs(&refs), [refs[1].clone()]);
}

#[test]
fn the_native_run_layout_is_a_worktree_root() {
    let tmp = tempfile::tempdir().unwrap();
    fs::create_dir_all(tmp.path().join("state/runs/run-a/worktree")).unwrap();
    let unit = |name: &str, path: PathBuf| UnitRef {
        unit: name.into(),
        path,
        exact: true,
    };
    let refs = vec![
        unit("a.service", tmp.path().join("state/runs/run-a/worktree/x")),
        unit("b.service", tmp.path().join("state/runs/run-b/worktree/x")),
    ];
    assert_eq!(missing_worktree_refs(&refs), [refs[1].clone()]);
}

#[test]
fn drop_ins_name_their_owner_and_a_missing_directory_names_nothing() {
    let tmp = tempfile::tempdir().unwrap();
    let dir = tmp.path().join("systemd/user");
    let drop_in = dir.join("liveness@router.service.d");
    fs::create_dir_all(&drop_in).unwrap();
    fs::write(
        drop_in.join("o.conf"),
        "[Service]\nWorkingDirectory=/wt/live-liveness\n",
    )
    .unwrap();
    fs::write(
        dir.join("md-sync.service"),
        "[Service]\n# ExecStart=/ignored\nExecStart=%h/code/main/run\n",
    )
    .unwrap();
    let refs = unit_file_refs(&dir, Path::new("/h")).unwrap();
    assert!(refs
        .iter()
        .any(|r| r.unit == "liveness@router.service" && r.path == Path::new("/wt/live-liveness")));
    assert!(refs
        .iter()
        .any(|r| r.unit == "md-sync.service" && r.path == Path::new("/h/code/main/run")));
    assert!(!protected(&refs, "/ignored"));
    assert!(unit_file_refs(&tmp.path().join("absent"), Path::new("/h"))
        .unwrap()
        .is_empty());
}

#[test]
fn a_single_valued_path_field_keeps_its_whole_value_with_spaces() {
    let refs = refs_of("[Service]\nWorkingDirectory=/wt/app-worktrees/team blue\n");
    assert!(exact(&refs, "/wt/app-worktrees/team blue"), "{refs:?}");
    let tree = Path::new("/wt/app-worktrees/team blue");
    assert_eq!(unit_names(&refs, tree, tree), ["t.service"]);
    // Quoted, or with a systemd prefix, it is the same one value.
    let refs = refs_of("[Service]\nEnvironmentFile=-\"/wt/app-worktrees/env blue/e\"\n");
    assert!(exact(&refs, "/wt/app-worktrees/env blue/e"), "{refs:?}");
    // A list field names one path per word.
    let refs = refs_of("[Service]\nReadWritePaths=/wt/app-worktrees/l1 /wt/app-worktrees/l2\n");
    assert!(
        exact(&refs, "/wt/app-worktrees/l1") && exact(&refs, "/wt/app-worktrees/l2"),
        "{refs:?}"
    );
}

#[test]
fn the_percent_escape_is_a_literal_percent_in_a_path() {
    let refs = refs_of("[Service]\nExecStart=/wt/app-worktrees/go%%ne/run\n");
    assert!(exact(&refs, "/wt/app-worktrees/go%ne/run"), "{refs:?}");
    // An unresolved specifier still names nothing.
    let refs = refs_of("[Service]\nExecStart=/wt/app-worktrees/%n/run\n");
    assert!(
        !refs.iter().any(|r| r.path.to_string_lossy().contains("%n")),
        "{refs:?}"
    );
}

#[cfg(unix)]
#[test]
fn symlinked_drop_in_directories_and_relative_link_targets_are_followed() {
    use std::os::unix::fs::symlink;
    let tmp = tempfile::tempdir().unwrap();
    let dir = tmp.path().join("systemd/user");
    let real = tmp.path().join("shared/app.service.d");
    fs::create_dir_all(&dir).unwrap();
    fs::create_dir_all(&real).unwrap();
    fs::write(
        real.join("o.conf"),
        "[Service]\nExecStart=/wt/app-worktrees/d1/run\n",
    )
    .unwrap();
    symlink(&real, dir.join("app.service.d")).unwrap();
    // A relative link target resolves against the link's directory.
    fs::create_dir_all(tmp.path().join("systemd/app-worktrees/d2")).unwrap();
    symlink(
        "../app-worktrees/d2/unit.service",
        dir.join("linked.service"),
    )
    .unwrap();
    // A directory link back to its parent must not loop forever.
    symlink(&dir, dir.join("loop.d")).unwrap();
    let refs = unit_file_refs(&dir, Path::new("/h")).unwrap();
    assert!(
        refs.iter()
            .any(|r| r.unit == "app.service" && r.path == Path::new("/wt/app-worktrees/d1/run")),
        "{refs:?}"
    );
    let linked = tmp
        .path()
        .join("systemd/app-worktrees/d2/unit.service")
        .canonicalize()
        .unwrap_or_else(|_| tmp.path().join("systemd/app-worktrees/d2/unit.service"));
    assert!(
        refs.iter()
            .any(|r| r.unit == "linked.service" && r.path == linked),
        "{refs:?}"
    );
}

#[cfg(unix)]
#[test]
fn a_dangling_link_target_with_spaces_only_protects() {
    use std::os::unix::fs::symlink;
    let tmp = tempfile::tempdir().unwrap();
    let dir = tmp.path().join("systemd/user");
    fs::create_dir_all(&dir).unwrap();
    symlink(
        "/wt/app-worktrees/gone blue/unit.service",
        dir.join("a.service"),
    )
    .unwrap();
    symlink(
        "/wt/app-worktrees/gone2/unit.service",
        dir.join("b.service"),
    )
    .unwrap();
    let refs = unit_file_refs(&dir, Path::new("/h")).unwrap();
    assert!(
        protected(&refs, "/wt/app-worktrees/gone blue/unit.service"),
        "{refs:?}"
    );
    assert!(
        !exact(&refs, "/wt/app-worktrees/gone blue/unit.service"),
        "{refs:?}"
    );
    assert!(
        exact(&refs, "/wt/app-worktrees/gone2/unit.service"),
        "{refs:?}"
    );
}

#[cfg(unix)]
#[test]
fn an_unreadable_parent_is_not_a_missing_worktree() {
    use std::os::unix::fs::PermissionsExt;
    let tmp = tempfile::tempdir().unwrap();
    let trees = tmp.path().join("app-worktrees");
    fs::create_dir_all(trees.join("alive")).unwrap();
    let refs = vec![UnitRef {
        unit: "a.service".into(),
        path: trees.join("alive/run"),
        exact: true,
    }];
    fs::set_permissions(&trees, fs::Permissions::from_mode(0o000)).unwrap();
    let reported = missing_worktree_refs(&refs);
    fs::set_permissions(&trees, fs::Permissions::from_mode(0o755)).unwrap();
    assert!(reported.is_empty(), "{reported:?}");
}

#[cfg(unix)]
#[test]
fn a_link_target_keeps_its_named_path_even_when_it_is_itself_a_link() {
    use std::os::unix::fs::symlink;
    let tmp = tempfile::tempdir().unwrap();
    let dir = tmp.path().join("systemd/user");
    let wt = tmp.path().join("app-worktrees/wt1");
    fs::create_dir_all(&dir).unwrap();
    fs::create_dir_all(&wt).unwrap();
    fs::create_dir_all(tmp.path().join("elsewhere")).unwrap();
    fs::write(tmp.path().join("elsewhere/real.service"), "[Service]\n").unwrap();
    symlink(
        tmp.path().join("elsewhere/real.service"),
        wt.join("unit.service"),
    )
    .unwrap();
    symlink(wt.join("unit.service"), dir.join("x.service")).unwrap();
    let refs = unit_file_refs(&dir, Path::new("/h")).unwrap();
    assert!(
        refs.iter()
            .any(|r| r.exact && r.path == wt.join("unit.service")),
        "{refs:?}"
    );
    let canonical = wt.canonicalize().unwrap();
    assert_eq!(
        unit_names(&refs, &wt, &canonical),
        ["x.service"],
        "{refs:?}"
    );
}

#[cfg(unix)]
#[test]
fn a_relative_link_in_a_linked_drop_in_directory_resolves_from_its_physical_parent() {
    use std::os::unix::fs::symlink;
    let tmp = tempfile::tempdir().unwrap();
    let dir = tmp.path().join("systemd/user");
    let real = tmp.path().join("shared/real.d");
    fs::create_dir_all(&dir).unwrap();
    fs::create_dir_all(&real).unwrap();
    symlink("../app-worktrees/d3/u.service", real.join("link.service")).unwrap();
    symlink(&real, dir.join("app.service.d")).unwrap();
    let refs = unit_file_refs(&dir, Path::new("/h")).unwrap();
    let expected = tmp.path().join("shared/app-worktrees/d3/u.service");
    assert!(
        refs.iter().any(|r| r.exact && r.path == expected),
        "{refs:?}"
    );
    assert!(
        !refs
            .iter()
            .any(|r| r.path == tmp.path().join("systemd/app-worktrees/d3/u.service")),
        "{refs:?}"
    );
}

#[cfg(unix)]
#[test]
fn two_units_sharing_a_linked_drop_in_directory_are_both_attributed() {
    use std::os::unix::fs::symlink;
    let tmp = tempfile::tempdir().unwrap();
    let dir = tmp.path().join("systemd/user");
    let common = tmp.path().join("shared/common.d");
    fs::create_dir_all(&dir).unwrap();
    fs::create_dir_all(&common).unwrap();
    fs::write(
        common.join("o.conf"),
        "[Service]\nExecStart=/wt/app-worktrees/shared1/run\n",
    )
    .unwrap();
    symlink(&common, dir.join("a.service.d")).unwrap();
    symlink(&common, dir.join("b.service.d")).unwrap();
    let refs = unit_file_refs(&dir, Path::new("/h")).unwrap();
    let tree = Path::new("/wt/app-worktrees/shared1");
    assert_eq!(
        unit_names(&refs, tree, tree),
        ["a.service", "b.service"],
        "{refs:?}"
    );
}

#[test]
fn a_decoded_control_byte_is_not_confused_with_a_percent_escape() {
    let refs = refs_of("[Service]\nExecStart=/wt/app-worktrees/a\\x01b/run\n");
    assert!(exact(&refs, "/wt/app-worktrees/a\u{1}b/run"), "{refs:?}");
    assert!(!protected(&refs, "/wt/app-worktrees/a%b/run"), "{refs:?}");
}

#[test]
fn percent_percent_before_h_is_a_literal_percent_not_the_home_specifier() {
    let refs = refs_of("[Service]\nExecStart=/wt/app-worktrees/release%%home/run\n");
    assert!(
        exact(&refs, "/wt/app-worktrees/release%home/run"),
        "{refs:?}"
    );
    // %h is still the home directory, also right after a literal percent.
    let refs = refs_of("[Service]\nExecStart=%h/code/run %%%h/x\n");
    assert!(exact(&refs, "/h/code/run"), "{refs:?}");
}

#[cfg(unix)]
#[test]
fn dot_dot_after_a_linked_directory_resolves_physically() {
    use std::os::unix::fs::symlink;
    let tmp = tempfile::tempdir().unwrap();
    let dir = tmp.path().join("systemd/user");
    let deep = tmp.path().join("other/deep");
    let live = tmp.path().join("other/wt-worktrees/live");
    fs::create_dir_all(&dir).unwrap();
    fs::create_dir_all(&deep).unwrap();
    fs::create_dir_all(&live).unwrap();
    symlink(&deep, dir.join("alias")).unwrap();
    // `alias/..` is `other` (the link's target parent), not `systemd/user`'s parent.
    symlink(
        "alias/../wt-worktrees/live/unit.service",
        dir.join("x.service"),
    )
    .unwrap();
    let refs = unit_file_refs(&dir, Path::new("/h")).unwrap();
    let canonical = live.canonicalize().unwrap();
    assert_eq!(
        unit_names(&refs, &live, &canonical),
        ["x.service"],
        "{refs:?}"
    );
    assert!(
        refs.iter()
            .any(|r| r.exact && r.path == canonical.join("unit.service")),
        "{refs:?}"
    );
}
