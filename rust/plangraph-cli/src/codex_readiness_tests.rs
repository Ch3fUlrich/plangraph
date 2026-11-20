use super::*;
#[test]
fn scratch_commit_is_isolated_from_inherited_git_metadata() {
    const CHILD: &str = "PG_READINESS_TEST_CHILD";
    if std::env::var_os(CHILD).is_some() {
        let repo = PathBuf::from(std::env::var_os("PG_READINESS_TEST_ROOT").unwrap());
        Host {
            repo: repo.clone(),
            mode: "danger-full-access".into(),
        }
        .commit()
        .unwrap();
        assert_eq!(std::fs::read_dir(repo).unwrap().count(), 0);
        return;
    }
    let root = tempfile::tempdir().unwrap();
    let sentinel = root.path().join("sentinel");
    std::fs::create_dir(&sentinel).unwrap();
    let work = root.path().join("work");
    std::fs::create_dir(&work).unwrap();
    // A global config pointing core.hooksPath at a marker-writing hook must not run.
    let hooks = root.path().join("hooks");
    let marker = root.path().join("hook-ran");
    std::fs::create_dir(&hooks).unwrap();
    let hook = hooks.join("pre-commit");
    std::fs::write(&hook, format!("#!/bin/sh\ntouch '{}'\n", marker.display())).unwrap();
    std::fs::set_permissions(&hook, std::os::unix::fs::PermissionsExt::from_mode(0o755)).unwrap();
    let global = root.path().join("gitconfig");
    std::fs::write(
        &global,
        format!("[core]\n\thooksPath = {}\n", hooks.display()),
    )
    .unwrap();
    let status = Command::new(std::env::current_exe().unwrap())
        .args([
            "--exact",
            "codex_readiness::tests::scratch_commit_is_isolated_from_inherited_git_metadata",
        ])
        .env(CHILD, "1")
        .env("PG_READINESS_TEST_ROOT", &work)
        .env("GIT_DIR", &sentinel)
        .env("GIT_WORK_TREE", &sentinel)
        .env("GIT_INDEX_FILE", sentinel.join("index"))
        .env("GIT_COMMON_DIR", &sentinel)
        .env("GIT_CONFIG_GLOBAL", &global)
        .env("GIT_CONFIG_COUNT", "1")
        .env("GIT_CONFIG_KEY_0", "core.hooksPath")
        .env("GIT_CONFIG_VALUE_0", &hooks)
        .status()
        .unwrap();
    assert!(status.success());
    assert!(
        !marker.exists(),
        "inherited git hooks ran in the scratch commit"
    );
    assert_eq!(std::fs::read_dir(sentinel).unwrap().count(), 0);
}
#[test]
fn sandbox_uses_process_cwd_without_profile_only_cd_flag() {
    let host = Host {
        repo: PathBuf::from("/repo"),
        mode: "workspace-write".into(),
    };
    let cmd = host.sandbox_command(Path::new("/scratch/work"), "git", &["status"]);
    assert_eq!(cmd.get_current_dir(), Some(Path::new("/scratch/work")));
    assert_eq!(
        cmd.get_args().collect::<Vec<_>>(),
        [
            "sandbox",
            "-c",
            "sandbox_mode=\"workspace-write\"",
            "--",
            "git",
            "status"
        ]
    );
}
#[test]
fn rpc_rejects_error_and_eof_without_model_requests() {
    let mut sent = Vec::new();
    let mut lines = vec![
        Ok(r#"{"method":"notice"}"#.into()),
        Ok(r#"{"id":2,"error":{}}"#.into()),
    ]
    .into_iter();
    assert!(rpc(&mut sent, &mut lines, 2, "config/read", json!({})).is_err());
    assert_eq!(
        serde_json::from_slice::<Value>(&sent).unwrap()["method"],
        "config/read"
    );
    assert!(rpc(
        &mut sent,
        &mut std::iter::empty(),
        3,
        "hooks/list",
        json!({})
    )
    .is_err());
}
#[test]
fn read_only_mode_skips_the_commit_point_but_writable_modes_probe_it() {
    let missing = PathBuf::from("/nonexistent/pg-readiness/repo");
    let mut host = Host {
        repo: missing.clone(),
        mode: "read-only".into(),
    };
    assert_eq!(host.check("commit"), Ok(()));
    host.mode = "workspace-write".into();
    assert!(host.check("commit").is_err());
}
#[test]
fn workspace_write_commit_passes_in_the_real_sandbox_when_it_can_start() {
    // Under HOME, not /tmp, which the sandbox always allows to write, and not
    // the crate dir, where a killed run would leave a repository in the source tree.
    let Some(home) = std::env::var_os("HOME") else {
        eprintln!("skipped: HOME is not set");
        return;
    };
    let parent = tempfile::Builder::new()
        .prefix(".pg-readiness-test-")
        .tempdir_in(home)
        .unwrap();
    let repo = parent.path().join("repo");
    std::fs::create_dir(&repo).unwrap();
    let mut host = Host {
        repo,
        mode: "workspace-write".into(),
    };
    if host.check("sandbox").is_err() {
        eprintln!("skipped: sandbox cannot start on this host");
        return;
    }
    host.commit().unwrap();
    assert_eq!(std::fs::read_dir(parent.path()).unwrap().count(), 1);
}

#[test]
fn placeholder_git_identities_are_not_ready() {
    for (name, email) in [
        ("x", "x@example.invalid"),
        ("Test", "test@example.com"),
        ("Dev", "dev@localhost"),
        ("", "dev@team.example.org.test"),
        ("Dev", ""),
    ] {
        assert!(identity_problem(name, email).is_some(), "{name} <{email}>");
    }
    assert_eq!(identity_problem("Dev Eloper", &usable()), None);
}
#[test]
fn identity_point_reads_the_hosts_git_config() {
    let dir = tempfile::tempdir().unwrap();
    let run = |args: &[&str]| {
        assert!(git()
            .current_dir(dir.path())
            .args(args)
            .status()
            .unwrap()
            .success());
    };
    run(&["init", "--quiet"]);
    let mut host = Host {
        repo: dir.path().to_path_buf(),
        mode: "workspace-write".into(),
    };
    run(&["config", "user.name", "x"]);
    run(&["config", "user.email", "x@example.invalid"]);
    assert!(host.check("identity").unwrap_err().contains("placeholder"));
    run(&["config", "user.email", &usable()]);
    assert_eq!(host.check("identity"), Ok(()));
    // The effective identity counts: a placeholder author override beats a usable user.email.
    run(&["config", "user.email", &usable()]);
    run(&["config", "author.email", "x@example.invalid"]);
    assert!(host
        .check("identity")
        .unwrap_err()
        .contains("GIT_AUTHOR_IDENT"));
    run(&["config", "--unset", "author.email"]);
    run(&["config", "committer.email", "x@example.invalid"]);
    assert!(host
        .check("identity")
        .unwrap_err()
        .contains("GIT_COMMITTER_IDENT"));
    run(&["config", "--unset", "committer.email"]);
    assert_eq!(host.check("identity"), Ok(()));
    run(&["config", "user.email", "x@example.invalid"]);
    // A read-only sandbox never commits, so its identity is not probed.
    run(&["config", "user.email", "x@example.invalid"]);
    host.mode = "read-only".into();
    assert_eq!(host.check("identity"), Ok(()));
}

/// An address on a real-looking domain, assembled so the file holds no address literal.
fn usable() -> String {
    ["dev", "@", "team.example.org"].concat()
}
