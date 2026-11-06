use super::*;
use tempfile::{tempdir, TempDir};

struct Fixture {
    tmp: TempDir,
    repo: RepoEntry,
    ws: Workspace,
}
impl Fixture {
    fn new() -> Self {
        let tmp = tempdir().unwrap();
        let root = tmp.path().join("repo");
        let trees = tmp.path().join("trees");
        fs::create_dir(&root).unwrap();
        fs::create_dir(&trees).unwrap();
        git(&root, &["init"]).unwrap();
        git(&root, &["config", "user.name", "Test"]).unwrap();
        git(&root, &["config", "user.email", "test@example.org"]).unwrap();
        git(&root, &["config", "core.autocrlf", "false"]).unwrap();
        git(&root, &["remote", "add", "origin", root.to_str().unwrap()]).unwrap();
        fs::write(root.join("tracked"), "initial\n").unwrap();
        git(&root, &["add", "."]).unwrap();
        git(&root, &["commit", "-m", "fixture"]).unwrap();
        let sha = git(&root, &["rev-parse", "HEAD"]).unwrap();
        Self {
            tmp,
            repo: RepoEntry {
                root: root.to_str().unwrap().into(),
                worktrees_root: trees.to_str().unwrap().into(),
                remote: root.to_str().unwrap().into(),
                python: None,
                import_roots: vec![".".into()],
                cache_env: Default::default(),
            },
            ws: Workspace {
                repo_id: "fixture".into(),
                mode: Mode::Writer,
                base_sha: Some(sha.clone()),
                revision_sha: Some(sha),
                branch: Some("lane/first".into()),
            },
        }
    }
    fn writer(&self) -> PathBuf {
        create_writer(&self.repo, &self.ws, "first").unwrap()
    }
    fn marker(&self, tree: &Path) -> PathBuf {
        git_path(tree, "--absolute-git-dir")
            .unwrap()
            .join("spawner-owned")
    }
}

#[test]
fn two_writers_have_distinct_fresh_lane_trees() {
    let mut f = Fixture::new();
    let first = f.writer();
    f.ws.branch = Some("lane/second".into());
    let second = create_writer(&f.repo, &f.ws, "second").unwrap();
    assert_ne!(first, second);
    for tree in [&first, &second] {
        verify_owned(&f.repo, tree).unwrap();
    }
    assert_eq!(
        git(&second, &["branch", "--show-current"]).unwrap(),
        "lane/second"
    );
    assert_eq!(
        git(&first, &["rev-parse", "HEAD"]).unwrap(),
        f.ws.base_sha.unwrap()
    );
}

#[test]
fn existing_branch_or_destination_refuses() {
    let mut f = Fixture::new();
    f.writer();
    assert!(create_writer(&f.repo, &f.ws, "other").is_err());
    f.ws.branch = Some("lane/other".into());
    assert!(create_writer(&f.repo, &f.ws, "first").is_err());
    assert!(!Path::new(&f.repo.worktrees_root).join("other").exists());
}

#[test]
fn run_id_traversal_refuses() {
    let f = Fixture::new();
    for id in ["", "..", "../escape", "a/b", "/absolute", "a.b"] {
        assert!(create_writer(&f.repo, &f.ws, id).is_err());
    }
}

#[test]
fn worktree_local_config_does_not_mutate_main() {
    let f = Fixture::new();
    let root = Path::new(&f.repo.root);
    git(root, &["config", "core.autocrlf", "true"]).unwrap();
    git(root, &["config", "core.longpaths", "false"]).unwrap();
    let tree = f.writer();
    assert_eq!(git(root, &["config", "core.autocrlf"]).unwrap(), "true");
    assert_eq!(git(root, &["config", "core.longpaths"]).unwrap(), "false");
    assert_eq!(git(&tree, &["config", "core.autocrlf"]).unwrap(), "false");
    #[cfg(windows)]
    assert_eq!(git(&tree, &["config", "core.longpaths"]).unwrap(), "true");
}

#[test]
fn marker_survives_clean() {
    let f = Fixture::new();
    let tree = f.writer();
    fs::write(tree.join("junk"), "discard").unwrap();
    verify_owned(&f.repo, &tree).unwrap();
    git(&tree, &["clean", "-ffdx"]).unwrap();
    assert!(!tree.join("junk").exists());
    verify_owned(&f.repo, &tree).unwrap();
}

#[test]
fn marker_in_checkout_missing_symlink_wrong_binding_refuses() {
    let f = Fixture::new();
    let tree = f.writer();
    let marker = f.marker(&tree);
    let bytes = fs::read(&marker).unwrap();
    fs::remove_file(&marker).unwrap();
    fs::write(tree.join("spawner-owned"), &bytes).unwrap();
    assert!(verify_owned(&f.repo, &tree).is_err());
    fs::write(&marker, "wrong").unwrap();
    assert!(verify_owned(&f.repo, &tree).is_err());
    #[cfg(unix)]
    {
        fs::remove_file(&marker).unwrap();
        std::os::unix::fs::symlink(tree.join("spawner-owned"), &marker).unwrap();
        assert!(verify_owned(&f.repo, &tree).is_err());
    }
}

#[test]
fn outside_root_foreign_common_dir_main_refuses() {
    let mut f = Fixture::new();
    let tree = f.writer();
    assert!(verify_owned(&f.repo, Path::new(&f.repo.root)).is_err());
    let other = Fixture::new();
    let foreign = other.writer();
    assert!(verify_owned(&f.repo, &foreign).is_err());
    f.repo.worktrees_root = other.repo.worktrees_root;
    assert!(verify_owned(&f.repo, &tree).is_err());
    assert!(verify_owned(&f.repo, &foreign).is_err());
}

#[test]
fn dirty_owned_tree_can_be_proven_without_reset() {
    let f = Fixture::new();
    let tree = f.writer();
    fs::write(tree.join("tracked"), "dirty").unwrap();
    verify_owned(&f.repo, &tree).unwrap();
    assert_eq!(fs::read_to_string(tree.join("tracked")).unwrap(), "dirty");
}

#[test]
fn child_sees_pinned_cwd_pwd_pythonpath_and_external_slot_cache() {
    let mut f = Fixture::new();
    let tree = f.writer();
    let cache = f.tmp.path().join("cache");
    fs::create_dir(&cache).unwrap();
    f.repo.cache_env.insert(
        "CARGO_TARGET_DIR".into(),
        cache.join("%SLOT%").to_str().unwrap().into(),
    );
    let mut cmd = Command::new(if cfg!(windows) { "python" } else { "sh" });
    #[cfg(unix)]
    cmd.args([
        "-c",
        "printf '%s\\n' \"$PWD\" \"$PYTHONPATH\" \"$CARGO_TARGET_DIR\"; pwd",
    ]);
    #[cfg(windows)]
    cmd.args(["-c", "import os; print(os.environ['PWD']); print(os.environ['PYTHONPATH']); print(os.environ['CARGO_TARGET_DIR']); print(os.getcwd())"]);
    crate::sandbox_env::confine_check(&mut cmd, f.tmp.path());
    apply_child_env(&mut cmd, &f.repo, &tree, "seat-one", f.tmp.path()).unwrap();
    assert_eq!(cmd.get_current_dir(), Some(tree.as_path()));
    let out = cmd.output().unwrap();
    assert!(out.status.success());
    let lines: Vec<_> = String::from_utf8(out.stdout)
        .unwrap()
        .lines()
        .map(str::to_owned)
        .collect();
    assert_eq!(
        lines,
        vec![
            tree.display().to_string(),
            tree.display().to_string(),
            cache
                .canonicalize()
                .unwrap()
                .join("seat-one")
                .display()
                .to_string(),
            tree.display().to_string()
        ]
    );
}

#[test]
fn all_inherited_and_explicit_git_vars_removed() {
    let f = Fixture::new();
    let tree = f.writer();
    let mut cmd = Command::new("unused");
    for key in [
        "GIT_DIR",
        "GIT_CONFIG_COUNT",
        "GIT_OBJECT_DIRECTORY",
        "git_custom",
    ] {
        cmd.env(key, "wrong");
    }
    apply_child_env(&mut cmd, &f.repo, &tree, "seat", f.tmp.path()).unwrap();
    for (key, value) in cmd.get_envs() {
        if key
            .to_string_lossy()
            .to_ascii_uppercase()
            .starts_with("GIT_")
        {
            assert!(value.is_none());
        }
    }
    for (key, _) in std::env::vars_os() {
        if key
            .to_string_lossy()
            .to_ascii_uppercase()
            .starts_with("GIT_")
        {
            assert!(cmd.get_envs().any(|(k, v)| k == key && v.is_none()));
        }
    }
}

#[test]
fn slot_cache_symlink_escape_and_reserved_env_refused() {
    let mut f = Fixture::new();
    let tree = f.writer();
    for (key, value) in [
        ("PWD", f.tmp.path().display().to_string()),
        ("CARGO_TARGET_DIR", tree.display().to_string()),
        ("CACHE", f.tmp.path().join("%OTHER%").display().to_string()),
    ] {
        f.repo.cache_env.clear();
        f.repo.cache_env.insert(key.into(), value);
        assert!(apply_child_env(
            &mut Command::new("unused"),
            &f.repo,
            &tree,
            "seat",
            f.tmp.path()
        )
        .is_err());
    }
    #[cfg(unix)]
    {
        let cache = f.tmp.path().join("cache");
        fs::create_dir(&cache).unwrap();
        std::os::unix::fs::symlink(&tree, cache.join("seat")).unwrap();
        f.repo.cache_env.clear();
        f.repo
            .cache_env
            .insert("CACHE".into(), cache.join("%SLOT%").display().to_string());
        assert!(apply_child_env(
            &mut Command::new("unused"),
            &f.repo,
            &tree,
            "seat",
            f.tmp.path()
        )
        .is_err());
    }
    assert!(apply_child_env(
        &mut Command::new("unused"),
        &f.repo,
        &tree,
        "../seat",
        f.tmp.path()
    )
    .is_err());
}

#[test]
fn none_has_no_checkout_and_no_cwd_fallback() {
    let mut f = Fixture::new();
    let tree = f.writer();
    f.ws.mode = Mode::None;
    f.ws.base_sha = None;
    f.ws.revision_sha = None;
    f.ws.branch = None;
    let mut cmd = Command::new("unused");
    assert!(
        prepare_launch(&mut cmd, &f.repo, &f.ws, None, "seat", f.tmp.path(), None)
            .unwrap()
            .is_none()
    );
    assert!(cmd.get_current_dir().is_none());
    assert!(!cmd.get_envs().any(|(k, _)| k == "PYTHONPATH"));
    assert!(prepare_launch(
        &mut cmd,
        &f.repo,
        &f.ws,
        Some(&tree),
        "seat",
        f.tmp.path(),
        None
    )
    .is_err());
}

#[test]
fn launch_reverifies_head_after_prior_token() {
    let f = Fixture::new();
    let tree = f.writer();
    verify_workspace(&f.repo, &f.ws, &tree, None).unwrap();
    git(
        &tree,
        &[
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.org",
            "commit",
            "--allow-empty",
            "-m",
            "changed",
        ],
    )
    .unwrap();
    assert!(prepare_launch(
        &mut Command::new("unused"),
        &f.repo,
        &f.ws,
        Some(&tree),
        "seat",
        f.tmp.path(),
        None
    )
    .is_err());
    assert!(prepare_launch(
        &mut Command::new("unused"),
        &f.repo,
        &f.ws,
        None,
        "seat",
        f.tmp.path(),
        None
    )
    .is_err());
}
