use super::*;
use std::fs;
use tempfile::{tempdir, TempDir};

struct Fixture {
    _tmp: TempDir,
    repo: RepoEntry,
    ws: Workspace,
    checkout: PathBuf,
}

impl Fixture {
    fn new() -> Self {
        let tmp = tempdir().unwrap();
        let root = tmp.path().join("repo");
        let trees = tmp.path().join("trees");
        fs::create_dir_all(&root).unwrap();
        fs::create_dir_all(&trees).unwrap();
        let run = |args: &[&str]| {
            let out = git_command(&root)
                .args(args)
                .env("GIT_AUTHOR_NAME", "Test")
                .env("GIT_AUTHOR_EMAIL", "test@example.org")
                .env("GIT_COMMITTER_NAME", "Test")
                .env("GIT_COMMITTER_EMAIL", "test@example.org")
                .output()
                .unwrap();
            assert!(out.status.success(), "{:?}", out.stderr);
        };
        run(&["init"]);
        run(&["config", "core.autocrlf", "false"]);
        run(&["config", "core.longpaths", "true"]);
        run(&["remote", "add", "origin", root.to_str().unwrap()]);
        fs::write(root.join("tracked"), "initial").unwrap();
        run(&["add", "."]);
        run(&["commit", "-m", "fixture"]);
        let sha = git(&root, &["rev-parse", "HEAD"]).unwrap();
        let checkout = trees.join("seat");
        run(&["worktree", "add", "--detach", checkout.to_str().unwrap()]);
        Self {
            _tmp: tmp,
            repo: RepoEntry {
                root: root.to_str().unwrap().into(),
                worktrees_root: trees.to_str().unwrap().into(),
                remote: root.to_str().unwrap().into(),
                python: None,
                import_roots: vec![],
                cache_env: Default::default(),
            },
            ws: Workspace {
                repo_id: "fixture".into(),
                mode: Mode::Read,
                base_sha: None,
                revision_sha: Some(sha),
                branch: None,
            },
            checkout,
        }
    }
    fn verify(&self) -> Result<VerifiedWorkspace, ResolveError> {
        verify_workspace(&self.repo, &self.ws, &self.checkout, None)
    }
    fn python(&mut self) {
        let python = if cfg!(windows) { "python" } else { "python3" };
        let venv = self._tmp.path().join("venv");
        assert!(Command::new(python)
            .args(["-m", "venv"])
            .arg(&venv)
            .status()
            .unwrap()
            .success());
        let executable = venv.join(if cfg!(windows) {
            "Scripts/python.exe"
        } else {
            "bin/python"
        });
        self.repo.python = Some(executable.to_str().unwrap().into());
        self.repo.import_roots = vec![".".into()];
    }
    fn import(&self, name: &str) -> Result<VerifiedWorkspace, ResolveError> {
        verify_workspace(&self.repo, &self.ws, &self.checkout, Some(name))
    }
}

#[test]
fn registry_toplevel_common_dir_remote_mismatch_each_refuses() {
    let mut f = Fixture::new();
    let token = f.verify().unwrap();
    assert_eq!(token.checkout(), f.checkout.canonicalize().unwrap());
    assert!(token.admin_dir().starts_with(token.common_dir()));
    assert_eq!(Some(token.revision()), f.ws.revision_sha.as_deref());
    let sub = f.checkout.join("sub");
    fs::create_dir(&sub).unwrap();
    assert!(verify_workspace(&f.repo, &f.ws, &sub, None).is_err());
    f.repo.remote.push_str("-wrong");
    assert!(f.verify().is_err());
    f.repo.remote = git(&f.checkout, &["remote", "get-url", "origin"]).unwrap();
    let other = Fixture::new();
    f.repo.root = other.repo.root;
    assert!(matches!(
        f.verify(),
        Err(ResolveError::Verify("common_dir"))
    ));
}

#[test]
fn main_checkout_refused() {
    let f = Fixture::new();
    assert!(verify_workspace(&f.repo, &f.ws, Path::new(&f.repo.root), None).is_err());
}

#[cfg(unix)]
#[test]
fn symlink_escape_refused() {
    let f = Fixture::new();
    let other = Fixture::new();
    let link = Path::new(&f.repo.worktrees_root).join("escape");
    std::os::unix::fs::symlink(&other.checkout, &link).unwrap();
    assert!(verify_workspace(&f.repo, &f.ws, &link, None).is_err());
}

#[test]
fn wrong_head_dirty_tracked_untracked_each_refuses() {
    let mut f = Fixture::new();
    let sha = f.ws.revision_sha.clone();
    f.ws.revision_sha = Some("0".repeat(40));
    assert!(f.verify().is_err());
    f.ws.revision_sha = sha;
    fs::write(f.checkout.join("tracked"), "changed").unwrap();
    assert!(f.verify().is_err());
    git(&f.checkout, &["restore", "tracked"]).unwrap();
    fs::write(f.checkout.join("untracked"), "new").unwrap();
    assert!(f.verify().is_err());
}

#[test]
fn writer_branch_and_detached_modes_checked() {
    let mut f = Fixture::new();
    f.ws.mode = Mode::Seat;
    f.ws.base_sha = f.ws.revision_sha.clone();
    assert!(f.verify().is_ok());
    git(&f.checkout, &["switch", "-c", "lane/test"]).unwrap();
    assert!(f.verify().is_err());
    f.ws.mode = Mode::Writer;
    f.ws.branch = Some("lane/test".into());
    assert!(f.verify().is_ok());
    f.ws.branch = Some("lane/other".into());
    assert!(f.verify().is_err());
    git(&f.checkout, &["checkout", "--detach"]).unwrap();
    assert!(f.verify().is_err());
}

#[test]
fn autocrlf_false_required() {
    let f = Fixture::new();
    for value in ["true", "input", "invalid"] {
        git(&f.checkout, &["config", "core.autocrlf", value]).unwrap();
        assert!(f.verify().is_err());
    }
    git(&f.checkout, &["config", "--unset", "core.autocrlf"]).unwrap();
    assert!(f.verify().is_err());
}

#[cfg(unix)]
#[test]
fn unix_longpaths_not_required() {
    let f = Fixture::new();
    git(&f.checkout, &["config", "--unset", "core.longpaths"]).unwrap();
    assert!(f.verify().is_ok());
}

#[cfg(windows)]
#[test]
fn windows_longpaths_true_required() {
    let f = Fixture::new();
    for value in ["false", "invalid"] {
        // Override parsing while repairing config: Git for Windows reads longpaths at startup.
        git(
            &f.checkout,
            &[
                "-c",
                "core.longpaths=true",
                "config",
                "core.longpaths",
                value,
            ],
        )
        .unwrap();
        assert!(f.verify().is_err());
    }
    git(
        &f.checkout,
        &[
            "-c",
            "core.longpaths=true",
            "config",
            "--unset",
            "core.longpaths",
        ],
    )
    .unwrap();
    assert!(f.verify().is_err());
}

#[test]
fn import_environment_removes_explicit_git_overrides() {
    let f = Fixture::new();
    let mut cmd = Command::new("unused");
    cmd.env("GIT_DIR", "wrong").env("GIT_WORK_TREE", "wrong");
    configure_import_environment(&mut cmd, &f.repo, &f.checkout).unwrap();
    for (key, value) in cmd.get_envs() {
        if key.to_string_lossy().starts_with("GIT_") {
            assert!(value.is_none());
        }
    }
}

#[test]
fn none_mode_never_falls_back_to_cwd() {
    let mut f = Fixture::new();
    f.ws.mode = Mode::None;
    f.ws.revision_sha = None;
    assert!(matches!(f.verify(), Err(ResolveError::Verify("none_mode"))));
}

#[test]
fn canary_inside_checkout_and_missing_file_bad_package_refused() {
    let mut f = Fixture::new();
    f.python();
    // Exclude generated fixture packages without modifying the tracked tree.
    fs::write(
        Path::new(&f.repo.root).join(".git/info/exclude"),
        "*.py\nnamespace/\n",
    )
    .unwrap();
    fs::write(f.checkout.join("sample.py"), "value = 1\n").unwrap();
    assert!(f.import("sample").is_ok());
    assert!(f.verify().is_err());
    for name in ["", "sample;exit()", "sample..x", "absent", "sys"] {
        assert!(f.import(name).is_err());
    }
    fs::create_dir(f.checkout.join("namespace")).unwrap();
    assert!(f.import("namespace").is_err());
}

#[test]
fn editable_pth_shadow_refused() {
    let mut f = Fixture::new();
    f.python();
    let outside = f._tmp.path().join("outside");
    fs::create_dir(&outside).unwrap();
    fs::write(outside.join("shadow.py"), "value = 1\n").unwrap();
    let output = Command::new(f.repo.python.as_ref().unwrap())
        .args(["-c", "import site; print(site.getsitepackages()[0])"])
        .output()
        .unwrap();
    let site = String::from_utf8(output.stdout).unwrap();
    fs::write(
        Path::new(site.trim()).join("editable.pth"),
        format!("{}\n", outside.display()),
    )
    .unwrap();
    assert!(f.import("shadow").is_err());
}

#[cfg(unix)]
#[test]
fn canary_timeout_refused() {
    use std::os::unix::fs::PermissionsExt;
    let mut f = Fixture::new();
    let script = f._tmp.path().join("python");
    let pidfile = f._tmp.path().join("child-pid");
    fs::write(
        &script,
        format!(
            "#!/bin/sh\ntrap '' TERM\nsleep 30 &\necho $! > '{}'\nwait\n",
            pidfile.display()
        ),
    )
    .unwrap();
    fs::set_permissions(&script, fs::Permissions::from_mode(0o700)).unwrap();
    f.repo.python = Some(script.to_str().unwrap().into());
    let started = std::time::Instant::now();
    assert!(f.import("sample").is_err());
    assert!(started.elapsed() >= Duration::from_secs(10));
    assert!(started.elapsed() < Duration::from_secs(20));
    let pid = fs::read_to_string(pidfile).unwrap();
    // A killed descendant can briefly remain a zombie until the host reaps it.
    let state = fs::read_to_string(format!("/proc/{}/stat", pid.trim()));
    if let Ok(state) = state {
        assert_eq!(state.split_whitespace().nth(2), Some("Z"));
    }
}
