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
    host.fast_checks_with_scrub(&assigned, None).unwrap();
    assert!(host.stamped(&assigned));

    git(&[
        "commit",
        "--allow-empty",
        "-m",
        "outside assignment\n\nPlan-Item: plangraph:UNASSIGNED",
    ]);
    let wrong = git(&["rev-parse", "HEAD"]);
    assert_eq!(
        host.fast_checks_with_scrub(&wrong, None)
            .unwrap_err()
            .to_string(),
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
pub(super) const RED_LINES: [&str; 3] = [
    "fixture::test_red",
    "fixture panic on stdout",
    "fixture failure on stderr",
];
const SANDBOX_NODES: [&str; 3] = [
    "codex_readiness::tests::workspace_write_commit_passes_in_the_real_sandbox_when_it_can_start",
    "native_writer_sandbox::tests::a_write_outside_the_named_roots_is_refused_in_a_fixture_run",
    "native_writer_sandbox::tests::a_writer_worktree_can_commit_with_the_named_git_directories",
];
const WORKER_NODE: &str = "sigkill_mid_attempt_and_mid_timer_stays_pinned";

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
    if cwd.join("fixture-red").exists() {
        let [node, panic, stderr] = RED_LINES;
        println!("test {node} ... FAILED\n{panic}");
        eprint!("{stderr}");
        std::process::exit(1);
    }
    let head = pg_worker_core::git::git(&cwd, &["rev-parse", "HEAD"]).unwrap();
    let check = pg_worker_core::fast_check_stamp::Check {
        label: "fake-fast-checks".into(),
        rc: 0,
        seconds: 0.1,
    };
    if cwd.join("fixture-skips").exists() {
        for node in SANDBOX_NODES
            .into_iter()
            .chain([SANDBOX_NODES[0], "unrelated"])
        {
            println!("test {node} ... ignored, live host check requiring a working Codex sandbox");
        }
        println!("PG_FAST_CHECK_SKIP {WORKER_NODE} reason=requires_temporal_deployment_fixture");
    }
    use pg_worker_core::fast_check_stamp::{publish_with_budget, BudgetOverrun};
    let budget = cwd
        .join("fixture-overrun")
        .exists()
        .then(|| BudgetOverrun::new(690_000_000_000, 300).unwrap());
    publish_with_budget(&cwd, &head, &[check], budget).unwrap();
}

#[test]
fn failing_shared_checks_preserve_streams_and_green_stamp_lists_skips() {
    let dir = tempfile::tempdir().unwrap();
    let mut host = Host::prepared_fixture(dir.path());
    let head = host.head().unwrap();
    let red = host.cwd.join("fixture-red");
    fs::write(&red, "").unwrap();
    let words = dir.path().join("words");
    fs::write(&words, "fixture").unwrap();
    fs::create_dir(host.cwd.join("tools")).unwrap();
    fs::write(host.cwd.join("tools/preflight_private_scrub.py"),
        "import pathlib, sys\nprint(pathlib.Path(sys.argv[sys.argv.index('--words') + 1]).resolve())\n").unwrap();
    let error = host
        .fast_checks_with_scrub(&head, Some(&words))
        .unwrap_err()
        .to_string();
    let stamp = host.stamp(&head).unwrap();
    let log = fs::read_to_string(stamp.with_extension("log")).unwrap();
    for text in [&error, &log] {
        for row in RED_LINES.into_iter().chain(["stdout:", "stderr:"]) {
            assert!(text.contains(row), "missing {row} in {text}");
        }
    }
    // The scrub stub prints the resolved --words path; compare canonical paths, since the
    // printed form differs from the tempdir path on Windows (short names, case, prefix).
    let canonical = fs::canonicalize(&words).unwrap();
    assert!(
        log.lines()
            .any(|line| fs::canonicalize(line.trim()).is_ok_and(|p| p == canonical)),
        "missing scrub words path in {log}"
    );
    assert!(!host.stamped(&head));
    fs::remove_file(&red).unwrap();
    fs::write(host.cwd.join("fixture-skips"), "").unwrap();
    for overrun in [false, true] {
        if overrun {
            fs::write(host.cwd.join("fixture-overrun"), "").unwrap();
        }
        host.fast_checks_with_scrub(&head, Some(&words)).unwrap();
        assert!(host.stamped(&head));
        let shared = pg_worker_core::worktree::fast_check_stamp(&host.cwd, &head).unwrap();
        let shared = fs::read_to_string(shared).unwrap();
        let reference = pg_worker_core::fast_check_stamp::local_receipt_ref(&head);
        let blob =
            pg_worker_core::git::git_bytes(&host.cwd, &["show", &format!("{reference}:{head}")])
                .unwrap();
        assert_eq!(blob, shared.as_bytes());
        assert_eq!(
            shared.contains("budget overrun wall_ns=690000000000 limit_s=300"),
            overrun
        );
        let mut skips =
            SANDBOX_NODES.map(|node| format!("skip {node} reason=requires_real_codex_sandbox"));
        skips.sort();
        assert_eq!(fs::read_to_string(&stamp).unwrap(), format!(
            "{shared}skips 4\n{}\nskip {WORKER_NODE} reason=requires_temporal_deployment_fixture\n", skips.join("\n")
        ));
    }
    fs::write(red, "").unwrap();
    assert!(host.fast_checks_with_scrub(&head, None).is_err());
    assert!(
        !host.stamped(&head),
        "a failed rerun must revoke its native stamp"
    );
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

#[cfg(unix)]
#[test]
fn a_failing_command_carries_its_stdout_and_stderr() {
    let error = output(Command::new("sh").args([
        "-c",
        "echo 'stdout line: gate output'; echo 'stderr line' >&2; exit 3",
    ]))
    .unwrap_err();
    assert!(error.contains("exit status: 3"), "{error}");
    assert!(
        error.contains("stdout: stdout line: gate output"),
        "{error}"
    );
    assert!(error.contains("stderr: stderr line"), "{error}");
    // A tool that prints nothing still names the exit status.
    let error = output(&mut Command::new("false")).unwrap_err();
    assert!(
        error.starts_with("command failed (exit status: 1)"),
        "{error}"
    );
}
/// The cards and briefs the feeder produced for two overlapping launches (the feeder test
/// `overlapping_feeder_launches_produce_...` pins this fixture to the feeder's real output).
const FEEDER_LAUNCHES: &str = include_str!("../tests/fixtures/feeder-overlap-launches.json");

/// Two overlapping feeder launches share a review directory. Each feeder card and brief goes
/// through native writer persistence; the review rendered from the saved record quotes only
/// the DONE lines of its own status file, the file the brief told the writer to append to.
#[cfg(unix)]
#[test]
fn reviews_rendered_from_saved_writer_records_quote_only_their_own_done_lines() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    let review = root.join("review");
    fs::create_dir_all(&review).unwrap();
    let launches: Vec<Value> = serde_json::from_str(FEEDER_LAUNCHES).unwrap();
    assert_eq!(launches.len(), 2);
    let prepared: Vec<(String, PathBuf, Value)> = launches
        .iter()
        .enumerate()
        .map(|(n, launch)| {
            // Place the feeder's review directory and claim epoch in this test's directory.
            let place = |text: String| {
                text.replace("/reviews", review.to_str().unwrap())
                    .replace("-eN.txt", &format!("-e{}.txt", 100 + n))
            };
            let card: Value = serde_json::from_str(&place(launch["card"].to_string())).unwrap();
            let brief = place(launch["brief"].as_str().unwrap().into());
            let payload: Value = serde_json::from_str(brief.split_once('\n').unwrap().1).unwrap();
            // The brief quotes the very file the card hands to the record.
            let status = card["writer_status_path"].as_str().unwrap().to_owned();
            assert_eq!(payload["run"]["status_file"], json!(status));
            // The writer appends one DONE line per assigned item to that file.
            let done: String = card["item_keys"]
                .as_array()
                .unwrap()
                .iter()
                .map(|k| format!("DONE {} abc\n", k.as_str().unwrap()))
                .collect();
            fs::write(&status, done).unwrap();
            let id = format!("run-feeder{n}");
            let record_path = root.join(format!("state/runs/{id}/record.json"));
            fs::create_dir_all(record_path.parent().unwrap()).unwrap();
            let request = root.join(format!("request-{n}.json"));
            fs::write(
                &request,
                json!({
                    "record": {"run_id": id, "repo": root, "base_ref": "refs/heads/main",
                               "tool_path": root.join("no-tools")},
                    "record_path": record_path,
                    "card": card,
                    "prompt": brief
                })
                .to_string(),
            )
            .unwrap();
            (request.to_string_lossy().into_owned(), record_path, card)
        })
        .collect();
    std::thread::scope(|scope| {
        for (request, _, _) in &prepared {
            scope.spawn(move || execute(vec![request.clone()]).unwrap());
        }
    });
    let src = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../src");
    for (_, record_path, card) in &prepared {
        let record = crate::records::read_record_file(record_path)
            .unwrap_or_else(|error| panic!("{}", error.into_error_data().message));
        assert_eq!(record["writer_status_path"], card["writer_status_path"]);
        let out = crate::test_python::command()
            .env("PYTHONPATH", &src)
            .args([
                "-c",
                "import json,sys\nfrom plangraph.review.prompt import render_brief\nprint(render_brief('review', {'writer_run': json.loads(sys.argv[1])}))",
                &record.to_string(),
            ])
            .output()
            .unwrap();
        assert!(
            out.status.success(),
            "{}",
            String::from_utf8_lossy(&out.stderr)
        );
        let brief = String::from_utf8(out.stdout).unwrap();
        let own = card["item_keys"][0].as_str().unwrap();
        let other = if own.contains("ALPHA") {
            "BETA"
        } else {
            "ALPHA"
        };
        assert!(brief.contains(&format!("DONE {own} abc")), "{brief}");
        assert!(!brief.contains(other), "{brief}");
    }
}
