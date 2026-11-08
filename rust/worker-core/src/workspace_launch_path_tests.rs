use super::*;

#[test]
fn git_worktree_paths_preserve_native_identity() {
    for path in [
        r"C:\trees\run",
        r"\\server\share\run",
        r"C:run",
        "relative/run",
        "/trees/run",
    ] {
        assert_eq!(git_worktree_path(Path::new(path)), Path::new(path));
    }
}

#[cfg(windows)]
#[test]
fn only_rooted_verbatim_disk_and_unc_paths_are_converted() {
    for (verbatim, native) in [
        (r"\\?\C:\trees\run", r"C:\trees\run"),
        (r"\\?\c:\trees with spaces\run", r"c:\trees with spaces\run"),
        (r"\\?\UNC\server\share\run", r"\\server\share\run"),
        (r"\\?\C:", r"\\?\C:"),
        (r"\\?\C:foo", r"\\?\C:foo"),
        (r"\\?\Volume{example}\run", r"\\?\Volume{example}\run"),
        (r"\\.\C:\run", r"\\.\C:\run"),
    ] {
        assert_eq!(git_worktree_path(Path::new(verbatim)), Path::new(native));
    }
}

#[test]
fn writer_rejects_win32_retargeting_leaves_before_git() {
    let f = tests::Fixture::new();
    for leaf in [
        "..",
        "../escape",
        "run.",
        "run ",
        "C:run",
        "C:",
        r"a\..\escape",
    ] {
        assert!(!safe_id(leaf));
        assert!(matches!(
            create_writer(&f.repo, &f.ws, leaf),
            Err(ResolveError::Verify("writer_run_id"))
        ));
    }
}
