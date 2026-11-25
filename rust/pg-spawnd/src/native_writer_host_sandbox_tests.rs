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
        fast_checks_stand_in: None,
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

/// Set by the test below on the parent process it starts; the probe runs only inside that process.
#[cfg(unix)]
const PARENT_PROBE_MARK: &str = "PG_FIXTURE_NATIVE_PARENT_PROBE";

/// Runs inside the isolated test parent: builds the production writer command around a fixture
/// Codex and records presence bits only (never a value or the rest of the environment).
#[cfg(unix)]
#[test]
fn native_child_environment_probe() {
    let Ok(report) = std::env::var(PARENT_PROBE_MARK) else {
        return;
    };
    let dir = tempfile::tempdir().unwrap();
    let (mut host, _, _) = fixture_host(&dir);
    let admitted = format!("/fixture/admitted's-bin:{}", std::env::var("PATH").unwrap());
    host.tool_path = admitted.clone();
    let fixture = dir.path().join("fixture-codex");
    // Values reach the generated script as single-quoted data, an apostrophe included.
    let quote = |value: &str| format!("'{}'", value.replace('\'', "'\\''"));
    let bit = |name: &str| {
        format!("[ -n \"${{{name}+x}}\" ] && echo {name}-present || echo {name}-absent\n")
    };
    pg_worker_core::executable_test_support::write_executable(
        &fixture,
        format!(
            "#!/bin/sh\n{{ {}{}{}[ \"$PATH\" = {} ] && echo path-admitted || echo path-other; }} > {}\n",
            bit("LANG"),
            bit("LC_FIXTURE_PASSWORD"),
            bit("PG_FIXTURE_PARENT_SECRET_TOKEN"),
            quote(&admitted),
            quote(&report),
        ),
    );
    let mut command = host
        .writer_command(&fixture, None, &dir.path().join("run"))
        .unwrap();
    assert!(command.status().unwrap().success());
}

/// A parent that carries a normal locale, an `LC_` look-alike and a keeper-shaped secret passes
/// only the exact allowed names, and the child's `PATH` is the admitted one, not the parent's.
#[cfg(unix)]
#[test]
fn a_locale_lookalike_in_parent_is_absent_in_the_native_child() {
    let dir = tempfile::tempdir().unwrap();
    let report = dir.path().join("child-report");
    // Test names carry the module path without the crate name.
    let probe = format!(
        "{}::native_child_environment_probe",
        module_path!().split_once("::").unwrap().1
    );
    let out = std::process::Command::new(std::env::current_exe().unwrap())
        .args(["--exact", &probe, "--test-threads=1"])
        .env(PARENT_PROBE_MARK, &report)
        .env("LANG", "C.UTF-8")
        .env("LC_FIXTURE_PASSWORD", "fixture-leak-value")
        .env("PG_FIXTURE_PARENT_SECRET_TOKEN", "fixture-leak-value")
        .output()
        .unwrap();
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stdout)
    );
    let seen = fs::read_to_string(&report).unwrap();
    assert_eq!(
        seen.lines().collect::<Vec<_>>(),
        [
            "LANG-present",
            "LC_FIXTURE_PASSWORD-absent",
            "PG_FIXTURE_PARENT_SECRET_TOKEN-absent",
            "path-admitted"
        ]
    );
}

#[cfg(unix)]
fn wide_request(dir: &Path, run_id: &str, sandbox: Value) -> (PathBuf, PathBuf) {
    let record_path = dir.join(format!("state/runs/{run_id}/record.json"));
    fs::create_dir_all(record_path.parent().unwrap()).unwrap();
    let request = record_path.with_file_name("native-request.json");
    let record = json!({"run_id":run_id, "repo":dir, "base_ref":"refs/heads/main",
        "tool_path":"/nonexistent", "status":"stopped", "session_id":"saved"});
    fs::write(
        &request,
        json!({"record":record, "record_path":record_path,
            "card":{"sandbox":sandbox}, "prompt":"brief"})
        .to_string(),
    )
    .unwrap();
    (record_path, request)
}

/// `Host::configure` refuses a card that asks for `danger-full-access` before Codex starts.
#[cfg(unix)]
#[test]
fn host_configure_refuses_danger_full_access_with_a_named_reason() {
    let dir = tempfile::tempdir().unwrap();
    let (record_path, request) = wide_request(dir.path(), "run-card", json!("danger-full-access"));
    let request = crate::records::read_record_file(&request)
        .ok()
        .expect("read native request fixture");
    let mut host = Host::from_request(
        &request,
        &json!({"cwd":dir.path(), "run_id":"run-card", "repo":dir.path(), "tool_path":"x"}),
        record_path,
    )
    .unwrap();
    let reason = host.configure(&request).unwrap_err();
    assert!(
        reason.starts_with("sandbox_refused: danger-full-access"),
        "{reason}"
    );
}

/// The saved request is checked under the supervisor guard before `take_resume` persists the
/// landing resume and releases the stop marker.
#[cfg(unix)]
#[test]
fn saved_native_request_refuses_danger_full_access() {
    let dir = tempfile::tempdir().unwrap();
    let (record_path, _) = wide_request(dir.path(), "run-saved", json!("danger-full-access"));
    fs::write(
        &record_path,
        json!({"run_id":"run-saved","status":"stopped","session_id":"s"}).to_string(),
    )
    .unwrap();
    let marker = record_path.with_file_name("landing-stop.json");
    fs::write(
        &marker,
        json!({"status":"stopped","session_id":"s","run_id":"run-saved"}).to_string(),
    )
    .unwrap();
    let (record_before, marker_before) =
        (fs::read(&record_path).unwrap(), fs::read(&marker).unwrap());
    let reason = resume_saved(vec![record_path.to_string_lossy().into_owned()]).unwrap_err();
    assert!(
        reason.starts_with("sandbox_refused: danger-full-access"),
        "{reason}"
    );
    assert_eq!(fs::read(&record_path).unwrap(), record_before);
    assert_eq!(fs::read(&marker).unwrap(), marker_before);
}

/// With an unusable work mount the disk selection would record `disk_low`; the card is refused first
/// and the terminal record, written through the lifecycle journal, names the sandbox.
#[cfg(unix)]
#[test]
fn direct_native_writer_danger_full_access_precedes_disk_selection() {
    const CHILD: &str = "PG_CARD_SANDBOX_DIRECT";
    if let Some(request) = std::env::var_os(CHILD) {
        execute(vec![request.to_string_lossy().into_owned()]).unwrap();
        return;
    }
    let dir = tempfile::tempdir().unwrap();
    let (record_path, request) =
        wide_request(dir.path(), "run-direct", json!("danger-full-access"));
    let output = std::process::Command::new(std::env::current_exe().unwrap())
        .args([
            "--exact",
            "native_writer_host::sandbox_tests::direct_native_writer_danger_full_access_precedes_disk_selection",
            "--nocapture",
        ])
        .env(CHILD, &request)
        .env("PLANGRAPH_WORKTREE_MOUNTS", dir.path().join("no-such-mount"))
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let record: Value = serde_json::from_slice(&fs::read(&record_path).unwrap()).unwrap();
    assert_eq!(record["status"], "refused", "{record}");
    assert_eq!(record["exit_code"], 5, "{record}");
    assert!(
        record["reason"]
            .as_str()
            .unwrap()
            .starts_with("sandbox_refused: danger-full-access"),
        "{record}"
    );
    assert!(record["ended"].is_number(), "{record}");
    assert!(
        dir.path().join("state/run-lifecycle").is_dir(),
        "lifecycle journal not written"
    );
}
