use super::*;

/// One spelling of a path for comparison: the canonical long form where it exists (else its parent's),
/// `\\?\` stripped, one separator, case-folded on Windows (8.3 names and separators differ by source).
fn same_path(path: &Path) -> String {
    let resolved = fs::canonicalize(path).unwrap_or_else(|_| {
        match (path.parent().map(fs::canonicalize), path.file_name()) {
            (Some(Ok(parent)), Some(name)) => parent.join(name),
            _ => path.to_path_buf(),
        }
    });
    let text = resolved.to_string_lossy().replace('\\', "/");
    let text = text.strip_prefix("//?/").unwrap_or(&text).to_owned();
    if cfg!(windows) {
        text.to_lowercase()
    } else {
        text
    }
}

fn fixture_host(dir: &tempfile::TempDir) -> (Host, PathBuf, PathBuf) {
    let repo = dir.path().join("repo");
    fs::create_dir(&repo).unwrap();
    let worktree = dir.path().join("worktree");
    let mut git = pg_worker_core::git_test_support::command(&repo);
    assert!(git.args(["init", "-q"]).status().unwrap().success());
    assert!(pg_worker_core::git_test_support::command(&repo)
        .args(["commit", "--allow-empty", "-qm", "base"])
        .status()
        .unwrap()
        .success());
    assert!(pg_worker_core::git_test_support::command(&repo)
        .args(["worktree", "add", "-q", "--detach"])
        .arg(&worktree)
        .status()
        .unwrap()
        .success());
    let host = Host {
        repo: repo.clone(),
        run_id: "run-sandbox".into(),
        cwd: worktree.clone(),
        reviews: dir.path().join("reviews"),
        record_path: dir.path().join("run/record.json"),
        base_ref: "refs/heads/main".into(),
        tool_path: std::env::var("PATH").unwrap(),
        effort: WRITER_EFFORT.into(),
        state: dir.path().to_path_buf(),
        brief: "brief".into(),
        push_ref: None,
        reviews_started: 0,
        review_override: None,
        review_cli: vec!["false".into()],
        python_src: None,
    };
    (host, repo, worktree)
}

#[test]
fn writer_command_is_workspace_write_and_never_danger_full_access() {
    let dir = tempfile::tempdir().unwrap();
    let (host, repo, worktree) = fixture_host(&dir);
    let git_dir = host.git(&["rev-parse", "--absolute-git-dir"]).unwrap();
    for session in [None, Some("session-1")] {
        let command = host
            .writer_command("codex", session, &dir.path().join("run"))
            .unwrap();
        let args: Vec<String> = command
            .get_args()
            .map(|a| a.to_string_lossy().into_owned())
            .collect();
        let line = args.join(" ");
        assert!(line.contains("sandbox_mode=\"workspace-write\""), "{line}");
        assert!(
            line.contains("sandbox_workspace_write.network_access=true"),
            "{line}"
        );
        assert!(!line.contains("danger-full-access"), "{line}");
        let roots: Vec<String> = serde_json::from_str(
            args.iter()
                .find_map(|a| a.strip_prefix("sandbox_workspace_write.writable_roots="))
                .unwrap(),
        )
        .unwrap();
        let roots: Vec<String> = roots.iter().map(|r| same_path(Path::new(r))).collect();
        for named in [
            dir.path().join("reviews"),
            dir.path().join("run"),
            repo.join(".git"),
            PathBuf::from(&git_dir),
        ] {
            assert!(
                roots.contains(&same_path(&named)),
                "{roots:?} lacks {named:?}"
            );
        }
        assert_eq!(args.contains(&"resume".to_owned()), session.is_some());
        let global = &args[..args
            .iter()
            .position(|arg| arg == "resume")
            .unwrap_or(args.len())];
        assert!(
            global
                .windows(2)
                .any(|pair| pair[0] == "--cd"
                    && same_path(Path::new(&pair[1])) == same_path(&worktree)),
            "{line}"
        );
        assert!(
            global.windows(2).any(|pair| pair[0] == "--add-dir"
                && same_path(Path::new(&pair[1])) == same_path(Path::new(&git_dir))),
            "{line}"
        );
    }
}

#[test]
fn writer_command_denies_reads_of_the_config_directory_and_the_ssh_directory() {
    let dir = tempfile::tempdir().unwrap();
    let (host, _, _) = fixture_host(&dir);
    let command = host
        .writer_command("codex", None, &dir.path().join("run"))
        .unwrap();
    let line = command
        .get_args()
        .map(|a| a.to_string_lossy().into_owned())
        .collect::<Vec<_>>()
        .join(" ");
    for path in pg_worker_core::credential_isolation::host_deny_read_paths().unwrap() {
        let entry = format!("\"{}\"=\"none\"", path.display());
        assert!(line.contains(&entry), "{entry} missing: {line}");
    }
    assert!(line.contains("/.config/plangraph\"=\"none\""), "{line}");
    assert!(line.contains("/.ssh\"=\"none\""), "{line}");
}

/// A fixture secret variable set in the parent never reaches the process `writer_command`
/// builds. The fixture reports only whether the variables arrived, never their values or the
/// rest of the environment.
#[cfg(unix)]
#[test]
fn a_parent_secret_variable_is_absent_in_the_writer_child() {
    let dir = tempfile::tempdir().unwrap();
    let (host, _, _) = fixture_host(&dir);
    let seen = dir.path().join("child-report");
    let fixture = dir.path().join("fixture-codex");
    pg_worker_core::executable_test_support::write_executable(
        &fixture,
        format!(
            "#!/bin/sh\n{{ [ -n \"${{PG_FIXTURE_PARENT_SECRET_TOKEN+x}}\" ] && echo secret-present || echo secret-absent\n\
             [ -n \"${{HOME+x}}\" ] && echo home-present || echo home-absent; }} > '{}'\n",
            seen.display()
        ),
    );
    std::env::set_var("PG_FIXTURE_PARENT_SECRET_TOKEN", "fixture-leak-value");
    let mut command = host
        .writer_command(&fixture, None, &dir.path().join("run"))
        .unwrap();
    assert!(command.status().unwrap().success());
    std::env::remove_var("PG_FIXTURE_PARENT_SECRET_TOKEN");
    let report = fs::read_to_string(&seen).unwrap();
    assert_eq!(
        report.lines().collect::<Vec<_>>(),
        ["secret-absent", "home-present"]
    );
}

/// The harness API key reaches Codex itself but not the commands the agent runs.
#[test]
fn writer_command_keeps_the_harness_api_key_out_of_the_agent_commands() {
    let dir = tempfile::tempdir().unwrap();
    let (host, _, _) = fixture_host(&dir);
    let command = host
        .writer_command("codex", None, &dir.path().join("run"))
        .unwrap();
    let line = command
        .get_args()
        .map(|a| a.to_string_lossy().into_owned())
        .collect::<Vec<_>>()
        .join(" ");
    assert!(
        line.contains("shell_environment_policy.exclude=[\"OPENAI_*\"]"),
        "{line}"
    );
}

/// A card that asks for `danger-full-access` ends the run before Codex starts.
#[cfg(unix)]
#[test]
fn a_card_asking_for_danger_full_access_exits_five_with_a_named_reason() {
    let dir = tempfile::tempdir().unwrap();
    let record_path = dir.path().join("state/runs/run-card/record.json");
    fs::create_dir_all(record_path.parent().unwrap()).unwrap();
    let request = dir.path().join("request.json");
    fs::write(
        &request,
        json!({"record":{"run_id":"run-card", "repo":dir.path(), "base_ref":"refs/heads/main",
            "tool_path":"/nonexistent"}, "record_path":record_path,
            "card":{"sandbox":"danger-full-access"}, "prompt":"brief"})
        .to_string(),
    )
    .unwrap();
    execute(vec![request.to_string_lossy().into_owned()]).unwrap();
    let record: Value = serde_json::from_slice(&fs::read(&record_path).unwrap()).unwrap();
    assert_eq!(record["exit_code"], 5, "{record}");
    assert!(
        record["reason"]
            .as_str()
            .unwrap()
            .starts_with("sandbox_refused: danger-full-access"),
        "{record}"
    );
    assert!(!dir.path().join("state/runs/run-card/native.log").exists());
}
