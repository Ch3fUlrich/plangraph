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

/// Two overlapping feeder launches share a review directory. Each native writer record is
/// saved from its own card; the review rendered from that saved record quotes only the DONE
/// lines of its own status file.
#[cfg(unix)]
#[test]
fn reviews_rendered_from_saved_writer_records_quote_only_their_own_done_lines() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    let review = root.join("review");
    fs::create_dir_all(&review).unwrap();
    let launches: Vec<(String, PathBuf, PathBuf)> = ["alpha", "beta"]
        .iter()
        .map(|name| {
            let id = format!("run-{name}");
            let record_path = root.join(format!("state/runs/{id}/record.json"));
            fs::create_dir_all(record_path.parent().unwrap()).unwrap();
            // The path the feeder reserved and handed to this launch; its writer appends here.
            let status = review.join(format!("status-{name}-1-aaaaaaaa-e100.txt"));
            fs::write(
                &status,
                format!("DONE plangraph:{}-1 {name}0000\n", name.to_uppercase()),
            )
            .unwrap();
            let request = root.join(format!("{name}-request.json"));
            fs::write(
                &request,
                json!({
                    "record": {"run_id": id, "repo": root, "base_ref": "refs/heads/main",
                               "tool_path": root.join("no-tools")},
                    "record_path": record_path,
                    "card": {"review_dir": review, "writer_status_path": status},
                    "prompt": "fixture"
                })
                .to_string(),
            )
            .unwrap();
            (request.to_string_lossy().into_owned(), record_path, status)
        })
        .collect();
    std::thread::scope(|scope| {
        for (request, _, _) in &launches {
            scope.spawn(move || execute(vec![request.clone()]).unwrap());
        }
    });
    let src = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../src");
    for (name, (_, record_path, status)) in ["alpha", "beta"].iter().zip(&launches) {
        let record = crate::records::read_record_file(record_path)
            .unwrap_or_else(|error| panic!("{}", error.into_error_data().message));
        assert_eq!(record["writer_status_path"], json!(status));
        let out = std::process::Command::new("python3")
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
        let other = if *name == "alpha" { "BETA" } else { "ALPHA" };
        assert!(
            brief.contains(&format!("DONE plangraph:{}-1", name.to_uppercase())),
            "{brief}"
        );
        assert!(!brief.contains(other), "{brief}");
    }
}
