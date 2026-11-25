use super::*;

pub(super) fn saved_prompt(host: &Host, head: &str, round: usize) -> String {
    let path = host
        .reviews
        .join(format!("review-{}-{round}.prompt.txt", &head[..8]));
    fs::read_to_string(path).unwrap()
}

const STATUS_LINE: &str = "SKIPPED plangraph:STATUS-ONLY Waiting for a fixture.";
const SLICES_CONTENT: &str = r#"{"items":[{"id":"DEFERRED-SLICE"}]}"#;

pub(super) fn save_writer_without_artifacts(host: &mut Host) {
    host.save(&json!({"run_id": host.run_id, "status": "running"}))
        .unwrap();
    let record = crate::records::read_record_file(&host.record_path)
        .unwrap_or_else(|error| panic!("{}", error.into_error_data().message));
    assert_eq!(
        record["writer_status_path"],
        json!(host.reviews.join("status.txt"))
    );
    assert_eq!(
        record["writer_slices_path"],
        json!(host.reviews.join("slices.json"))
    );
    assert!(!host.reviews.join("status.txt").exists());
    assert!(!host.reviews.join("slices.json").exists());
}

pub(super) fn write_writer_artifacts(host: &Host) {
    let status = host.reviews.join("status.txt");
    let slices = host.reviews.join("slices.json");
    fs::write(&status, format!("{STATUS_LINE}\n")).unwrap();
    fs::write(&slices, SLICES_CONTENT).unwrap();
}

#[cfg(unix)]
#[test]
fn native_request_saves_explicit_writer_artifact_paths() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    let record_path = root.join("state/runs/run-artifacts/record.json");
    fs::create_dir_all(record_path.parent().unwrap()).unwrap();
    let status = root.join("review").join("writer-summary.txt");
    let slices = root.join("review").join("writer-deferred.json");
    let request = root.join("request.json");
    fs::write(
        &request,
        json!({
            "record": {
                "run_id": "run-artifacts",
                "repo": root,
                "base_ref":"refs/heads/main",
                "tool_path": root.join("no-tools"),
            },
            "record_path": record_path,
            "card": {
                "review_dir": root.join("review"),
                "writer_status_path": status,
                "writer_slices_path": slices,
            },
            "prompt": "fixture"
        })
        .to_string(),
    )
    .unwrap();
    // Missing tools stop execution before Codex starts, after the run is recorded.
    execute(vec![request.to_string_lossy().into_owned()]).unwrap();
    let record = crate::records::read_record_file(&record_path)
        .unwrap_or_else(|error| panic!("{}", error.into_error_data().message));
    assert_eq!(record["writer_status_path"], json!(status));
    assert_eq!(record["writer_slices_path"], json!(slices));
    assert!(!record_path.with_file_name("native.log").exists());
}

pub(super) fn assert_writer_artifacts(host: &Host, head: &str) {
    let prompt = saved_prompt(host, head, 2);
    assert!(prompt.contains(&format!("> {STATUS_LINE}")));
    assert!(prompt.contains(&format!("> {SLICES_CONTENT}")));
}

pub(super) fn assert_item_trailers_bound_to_brief(
    host: &mut Host,
    git: impl Fn(&[&str]) -> String,
) {
    host.brief
        .push_str("\n## plangraph:ASSIGNED | t\nWhy: depends on plangraph:UNASSIGNED\n");
    git(&[
        "commit",
        "--allow-empty",
        "-m",
        "assigned item\n\nPlan-Item: plangraph:ASSIGNED",
    ]);
    let assigned = git(&["rev-parse", "HEAD"]);
    host.fast_checks(&assigned).unwrap();
    assert!(host.stamped(&assigned));

    git(&[
        "commit",
        "--allow-empty",
        "-m",
        "outside assignment\n\nPlan-Item: plangraph:UNASSIGNED",
    ]);
    let wrong = git(&["rev-parse", "HEAD"]);
    assert_eq!(
        host.fast_checks(&wrong).unwrap_err(),
        format!("commit {wrong}: Plan-Item outside writer brief: plangraph:UNASSIGNED")
    );
    assert!(!host.stamped(&wrong));
}

#[test]
fn same_head_re_review_diffs_against_the_batch_base() {
    use super::review_diff_base;
    let (base, old, head) = ("b".repeat(40), "a".repeat(40), "c".repeat(40));
    // A later head reviews the delta from the previous review's head.
    assert_eq!(review_diff_base(&base, &head, Some((&old, "text"))), old);
    // The same head must never ask for the empty delta head..head.
    assert_eq!(review_diff_base(&base, &head, Some((&head, "text"))), base);
    assert_eq!(review_diff_base(&base, &head, None), base);
}

#[test]
fn tool_dependent_review_instructions_sit_inside_the_tools_only_block() {
    use super::review::{TOOLS_ONLY_CLOSE, TOOLS_ONLY_OPEN};
    let dir = tempfile::tempdir().unwrap();
    let mut host = Host {
        repo: dir.path().into(),
        cwd: dir.path().into(),
        run_id: "run-tools".into(),
        reviews: dir.path().join("reviews"),
        record_path: dir.path().join("record.json"),
        base_ref: "refs/heads/main".into(),
        tool_path: std::env::var("PATH").unwrap(),
        effort: WRITER_EFFORT.into(),
        state: dir.path().to_path_buf(),
        brief: "the item brief".into(),
        push_ref: None,
        fast_checks_stand_in: None,
        reviews_started: 0,
        review_override: None,
        review_cli: vec!["false".into()],
        python_src: None,
    };
    host.git(&["init", "-q"]).unwrap();
    let commit = |message| {
        host.git(&[
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "--allow-empty",
            "-qm",
            message,
        ])
        .unwrap();
        host.git(&["rev-parse", "HEAD"]).unwrap()
    };
    let base = commit("base");
    let earlier = commit("previous writer round");
    let head = commit("current writer round");
    for (previous, diff_base) in [
        (None, &base),
        (Some((earlier.as_str(), "previous review")), &earlier),
        (Some((head.as_str(), "previous review")), &base),
    ] {
        let prompt = host
            .review_prompt(
                &base,
                &head,
                previous,
                &["openai".to_owned()],
                "VERSIONED BRIEF",
                "A nonempty review skill.",
            )
            .unwrap();
        assert!(prompt.starts_with(TOOLS_ONLY_OPEN));
        let (block, outside) = prompt.split_once(TOOLS_ONLY_CLOSE).unwrap();
        assert!(
            block.contains("run read-only commands and the tests"),
            "{block}"
        );
        let command = format!("git diff {diff_base} {head}");
        assert!(block.lines().any(|line| line == command), "{block}");
        assert!(outside.contains("Writer brief"), "{outside}");
        assert!(
            !outside.contains("run read-only commands")
                && !outside.contains("Open callers")
                && !outside.contains("git diff "),
            "{outside}"
        );
    }
}

#[test]
fn review_tests_evidence_distinguishes_untested_accepts() {
    let head = "b".repeat(40);
    let paths = ["src/lib.rs".to_owned()];
    for (evidence, expected) in [
        ("", false),
        ("TESTS: none: Bash denied\n", false),
        (
            "```text\nTESTS: cargo test -p fixture — 1 passed\n```\n",
            false,
        ),
        ("TESTS: cargo test -p fixture — 1 passed\n", true),
    ] {
        let text = format!("FILES READ: src/lib.rs\n{evidence}VERDICT: ACCEPT\n");
        let review = review_verdict(&text, &head, &paths);
        assert_eq!(review["verdict"], "ACCEPT");
        assert_eq!(review["tested"], expected, "{text}");
    }
}

#[test]
fn native_sonnet_pass_requires_reported_tests() {
    let state = tempfile::tempdir().unwrap();
    let head = "b".repeat(40);
    let paths = ["src/lib.rs".to_owned()];
    for (summary, expected) in [
        ("permission denied", "no_verdict"),
        ("1 passed in 0.1s", "ACCEPT"),
    ] {
        let record = json!({
            "head_sha": head,
            "host": "fixture",
            "overflow_remote": true,
            "chosen_leg": {"harness":"claude","model":"claude-sonnet-5-5-high"},
            "stdout_tail": format!("FILES READ: src/lib.rs\nTESTS: pytest -q tests/test_fixture.py — {summary}\nVERDICT: ACCEPT"),
        });
        let verdict = review_record_verdict(state.path(), &record, &head, &paths, None).unwrap();
        assert_eq!(verdict["verdict"], expected, "{verdict}");
    }
}

/// Set on the stand-in's child process only (by `Host::fast_checks_command`): the test harness's
/// own process never has it, so the stand-in test below is a no-op there.
pub(super) const STAND_IN_MARKER: &str = "PG_FAKE_FAST_CHECKS";
const STAND_IN_TEST: &str = "native_writer_host::review_tests::fast_checks_stand_in";

/// What a test host runs in place of the shared exact-head fast checks (the real command is tested
/// in plangraph-cli), on every platform: this test executable runs again, as the policy and umask
/// tests of this module do, and that run publishes the receipt through the real producer.
pub(super) fn stand_in_argv() -> Option<Vec<String>> {
    Some(vec![
        std::env::current_exe()
            .unwrap()
            .to_string_lossy()
            .into_owned(),
        "--exact".into(),
        STAND_IN_TEST.into(),
        "--nocapture".into(),
    ])
}

/// The stand-in's own run: publishes the receipt of HEAD in the current directory when the host
/// marked it; nothing when the harness runs it as an ordinary test.
#[test]
fn fast_checks_stand_in() {
    if std::env::var_os(STAND_IN_MARKER).is_none() {
        return;
    }
    let cwd = std::env::current_dir().unwrap();
    let head = pg_worker_core::git::git(&cwd, &["rev-parse", "HEAD"]).unwrap();
    let check = pg_worker_core::fast_check_stamp::Check {
        label: "fake-fast-checks".into(),
        rc: 0,
        seconds: 0.1,
    };
    pg_worker_core::fast_check_stamp::publish(&cwd, &head, &[check]).unwrap();
}

/// The fast-check receipt is the shared producer's (structured, in the shared state and as one
/// parentless commit), and the push sends the head and its per-lane receipt ref in one atomic push.
pub(super) fn assert_shared_receipt_and_push(host: &mut Host, dir: &Path, head: &str) {
    let cwd = host.cwd.clone();
    let receipt = fs::read_to_string(host.stamp(head).unwrap()).unwrap();
    assert!(
        receipt.starts_with(&format!(
            "head {head}\nresult green\ncheck fake-fast-checks rc=0 "
        )),
        "{receipt}"
    );
    assert!(pg_worker_core::fast_check_stamp::green(&cwd, head));
    let remote = dir.join("receipts-remote.git");
    host.git(&["init", "-q", "--bare", remote.to_str().unwrap()])
        .unwrap();
    host.git(&["remote", "set-url", "origin", remote.to_str().unwrap()])
        .unwrap();
    host.push_ref = Some("refs/heads/lane/h".into());
    host.push(head).unwrap();
    let tip = pg_worker_core::git::git(
        &remote,
        &["rev-parse", "refs/notes/plangraph-fast-checks-lane%2fh"],
    )
    .unwrap();
    assert_eq!(
        pg_worker_core::fast_check_stamp::notes_commit_green(&remote, &tip, Some(head)),
        Ok(())
    );
    assert_eq!(
        pg_worker_core::git::git(&remote, &["rev-parse", "refs/heads/lane/h"]).unwrap(),
        head
    );
    host.push_ref = None;
}

/// A fresh feeder launch renders its brief before the worktree exists: the gate paths come from
/// the host repository, not from the display `WORKTREE` sentence or the missing worktree.
#[test]
fn a_fresh_feeder_launch_renders_the_gate_paths_before_its_worktree_exists() {
    let dir = tempfile::tempdir().unwrap();
    let repo = dir.path().join("repo");
    fs::create_dir_all(repo.join("tools")).unwrap();
    fs::write(
        repo.join("tools/ci_commit_gate.py"),
        "GATE_PATHS: tuple[str, ...] = (\n    'tools/ci_commit_gate.py',\n    '.github/workflows/*',\n)\n",
    )
    .unwrap();
    let record_path = dir.path().join("state/runs/run-fresh/record.json");
    let record = json!({"run_id":"run-fresh", "repo":repo, "base_ref":"refs/heads/main",
        "tool_path":"/nonexistent", "cwd":dir.path().join("worktree-not-yet-created")});
    let request = json!({"record":record, "record_path":record_path, "card":{},
        "prompt": include_str!("../tests/fixtures/feeder-batch-input.txt")});
    let mut host = Host::from_request(&request, &record, record_path).unwrap();
    host.configure(&request).unwrap();
    let line = host
        .brief
        .lines()
        .find(|line| line.starts_with("GATE_PATHS "))
        .unwrap_or_else(|| panic!("no GATE_PATHS line in {}", host.brief));
    assert!(line.contains("tools/ci_commit_gate.py") && line.contains(".github/workflows/*"));
}
