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
