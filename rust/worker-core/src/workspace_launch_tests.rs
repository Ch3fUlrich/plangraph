use super::*;
use tempfile::{tempdir, TempDir};
#[test]
fn none_missing_worktrees_root() {
    let mut f = Fixture::new();
    f.repo.worktrees_root = f.tmp.path().join("missing/trees").to_str().unwrap().into();
    f.ws = none_workspace();
    assert!(matches!(
        launch(&f, &mut Command::new("unused"), None),
        Err(ResolveError::Verify("worktrees_root"))
    ));
    assert!(!f.tmp.path().join("missing").exists());
}
#[test]
fn none_file_worktrees_root() {
    let mut f = Fixture::new();
    let file = f.tmp.path().join("not-a-directory");
    fs::write(&file, "unchanged").unwrap();
    f.repo.worktrees_root = file.to_str().unwrap().into();
    f.ws = none_workspace();
    assert!(matches!(
        launch(&f, &mut Command::new("unused"), None),
        Err(ResolveError::Verify("worktrees_root"))
    ));
    assert_eq!(fs::read_to_string(file).unwrap(), "unchanged");
}
#[test]
fn none_existing_worktrees_root() {
    let mut f = Fixture::new();
    f.ws = none_workspace();
    assert!(launch(&f, &mut Command::new("unused"), None)
        .unwrap()
        .is_none());
    f.repo.worktrees_root = f.tmp.path().to_str().unwrap().into();
    assert!(launch(&f, &mut Command::new("unused"), None).is_err());
}
fn none_workspace() -> Workspace {
    Workspace {
        repo_id: "fixture".into(),
        mode: Mode::None,
        base_sha: None,
        revision_sha: None,
        branch: None,
    }
}
fn apply(f: &Fixture, tree: &Path) -> Result<(), ResolveError> {
    let mut cmd = Command::new("unused");
    apply_child_env(&mut cmd, &f.repo, tree, "seat", f.tmp.path())
}
pub(super) fn launch(
    f: &Fixture,
    cmd: &mut Command,
    tree: Option<&Path>,
) -> Result<Option<VerifiedWorkspace>, ResolveError> {
    prepare_launch(cmd, &f.repo, &f.ws, tree, "seat", f.tmp.path(), None)
}
pub(super) struct Fixture {
    pub(super) tmp: TempDir,
    pub(super) repo: RepoEntry,
    pub(super) ws: Workspace,
}
impl Fixture {
    pub(super) fn new() -> Self {
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
                live_dir: None,
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
    pub(super) fn writer(&self) -> PathBuf {
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
    git(root, &["config", "core.repositoryformatversion", "0"]).unwrap();
    git(root, &["config", "core.autocrlf", "true"]).unwrap();
    git(root, &["config", "core.longpaths", "false"]).unwrap();
    let tree = f.writer();
    assert_eq!(fs::read(tree.join("tracked")).unwrap(), b"initial\n");
    assert_eq!(git(&tree, &["status", "--porcelain"]).unwrap(), "");
    verify_workspace(&f.repo, &f.ws, &tree, None).unwrap();
    let format = git(root, &["config", "core.repositoryformatversion"]).unwrap();
    assert_eq!(format, "0");
    assert_eq!(git(root, &["config", "core.autocrlf"]).unwrap(), "true");
    assert_eq!(git(root, &["config", "core.longpaths"]).unwrap(), "false");
    assert_eq!(git(&tree, &["config", "core.autocrlf"]).unwrap(), "false");
    #[cfg(windows)]
    assert_eq!(git(&tree, &["config", "core.longpaths"]).unwrap(), "true");
}
#[test]
fn destination_created_while_waiting_for_lock_refuses() {
    let f = Fixture::new();
    let common = git_path(Path::new(&f.repo.root), "--git-common-dir").unwrap();
    let lock = common.join("spawner-config.lock");
    let guard = lockfile::try_lock(&lock).unwrap().unwrap();
    std::thread::scope(|scope| {
        let pending = scope.spawn(|| create_writer(&f.repo, &f.ws, "first"));
        std::thread::sleep(Duration::from_millis(500));
        fs::create_dir(Path::new(&f.repo.worktrees_root).join("first")).unwrap();
        drop(guard);
        let result = pending.join().unwrap();
        let expected: Result<PathBuf, _> = Err(ResolveError::Verify("destination_exists"));
        assert_eq!(format!("{result:?}"), format!("{expected:?}"));
    });
}
#[test]
fn temp_inside_worktrees_refuses() {
    let f = Fixture::new();
    let tree = f.writer();
    for temp in [Path::new(&f.repo.worktrees_root), tree.as_path()] {
        let result = apply_child_env(&mut Command::new("unused"), &f.repo, &tree, "seat", temp);
        assert!(result.is_err());
    }
}
#[test]
#[cfg(unix)]
fn plain_command_confines_secret() {
    let secret = ["KEEPER", "_DATABASE", "_TOKEN"].concat();
    if std::env::var_os("WORKSPACE_ENV_PROBE").is_none() {
        let out = Command::new(std::env::current_exe().unwrap())
            .arg("--exact")
            .arg("workspace_launch::tests::plain_command_confines_secret")
            .env("WORKSPACE_ENV_PROBE", "1")
            .env(&secret, "fake-canary")
            .output()
            .unwrap();
        assert!(out.status.success());
        return;
    }
    let f = Fixture::new();
    let tree = f.writer();
    let mut cmd = Command::new("/bin/sh");
    cmd.args(["-c", &format!("test -z \"${secret}\"")]);
    cmd.env_remove("PATH");
    apply_child_env(&mut cmd, &f.repo, &tree, "seat", f.tmp.path()).unwrap();
    let path_set = cmd.get_envs().any(|(k, v)| k == "PATH" && v.is_some());
    assert!(!path_set);
    assert!(cmd.output().unwrap().status.success());
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
    assert!(matches!(
        verify_owned(&f.repo, &foreign),
        Err(ResolveError::Verify("common_dir"))
    ));
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
    if std::env::var_os("PINNED_ENV_PROBE").is_none() {
        let out = Command::new(std::env::current_exe().unwrap())
            .args(["--exact", "workspace_launch::tests::child_sees_pinned_cwd_pwd_pythonpath_and_external_slot_cache"])
            .env("PINNED_ENV_PROBE", "1")
            .env("PYTHONPATH", "wrong").env("TMPDIR", std::env::temp_dir())
            .env("TMP", std::env::temp_dir()).env("PYTEST_ADDOPTS", "wrong")
            .output().unwrap();
        assert!(
            out.status.success(),
            "{}",
            String::from_utf8_lossy(&out.stdout)
        );
        return;
    }
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
        "printf '%s\\n' \"$PWD\" \"$PYTHONPATH\" \"$CARGO_TARGET_DIR\" \"$PYTHONDONTWRITEBYTECODE\"; pwd",
    ]);
    #[cfg(windows)]
    cmd.args(["-c", "import os; print(os.environ['PWD']); print(os.environ['PYTHONPATH']); print(os.environ['CARGO_TARGET_DIR']); print(os.environ['PYTHONDONTWRITEBYTECODE']); print(os.getcwd())"]);
    crate::sandbox_env::confine_check(&mut cmd, f.tmp.path());
    cmd.env("PYTHONDONTWRITEBYTECODE", "0");
    apply_child_env(&mut cmd, &f.repo, &tree, "seat-one", f.tmp.path()).unwrap();
    let temp = f.tmp.path().canonicalize().unwrap();
    let cache = cache.canonicalize().unwrap();
    let pytest = std::ffi::OsStr::new("-p no:cacheprovider");
    for (key, expected) in [
        ("TMPDIR", temp.as_os_str()),
        ("TEMP", temp.as_os_str()),
        ("TMP", temp.as_os_str()),
        ("PYTEST_ADDOPTS", pytest),
    ] {
        assert!(cmd.get_envs().any(|(k, v)| k == key && v == Some(expected)));
    }
    assert_eq!(cmd.get_current_dir(), Some(tree.as_path()));
    let out = cmd.output().unwrap();
    assert!(out.status.success());
    let stdout = String::from_utf8(out.stdout).unwrap();
    let lines: Vec<_> = stdout.lines().map(str::to_owned).collect();
    assert_eq!(lines.len(), 5);
    assert_eq!(Path::new(&lines[0]).canonicalize().unwrap(), tree);
    assert_eq!(
        std::env::split_paths(&lines[1]).collect::<Vec<_>>(),
        vec![tree.clone()]
    );
    assert_eq!(Path::new(&lines[2]), cache.join("seat-one"));
    assert_eq!(lines[3], "1");
    assert_eq!(Path::new(&lines[4]).canonicalize().unwrap(), tree);
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
            assert!(!cmd.get_envs().any(|(k, v)| k == key && v.is_some()));
        }
    }
}
#[test]
fn slot_cache_symlink_escape_and_reserved_env_refused() {
    let mut f = Fixture::new();
    let tree = f.writer();
    for (key, value) in [
        ("PWD", f.tmp.path().display().to_string()),
        ("DYLD_INSERT_LIBRARIES", f.tmp.path().display().to_string()),
        ("DYLD_OTHER", f.tmp.path().display().to_string()),
        ("LD_AUDIT", f.tmp.path().display().to_string()),
        ("RUSTC_WRAPPER", f.tmp.path().display().to_string()),
        ("BASH_ENV", f.tmp.path().display().to_string()),
        ("CARGO_TARGET_DIR", tree.display().to_string()),
        ("CACHE", f.tmp.path().join("%OTHER%").display().to_string()),
    ] {
        f.repo.cache_env.clear();
        f.repo.cache_env.insert(key.into(), value);
        assert!(apply(&f, &tree).is_err());
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
        assert!(apply(&f, &tree).is_err());
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
fn workspace_launch_docs_avoid_credential_shaped_git_wording() {
    let docs = include_str!("../../../docs/workspace-resolver.md");
    assert!(!docs
        .lines()
        .any(|line| line.contains("explicit") && line.contains("GIT_*")));
}
