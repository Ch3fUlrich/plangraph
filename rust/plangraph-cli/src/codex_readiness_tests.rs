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
        .status()
        .unwrap();
    assert!(status.success());
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
    // Under the crate dir, not /tmp, which the sandbox always allows to write.
    let parent = tempfile::tempdir_in(env!("CARGO_MANIFEST_DIR")).unwrap();
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
