use super::*;

pub(super) fn saved_prompt(host: &Host, head: &str, round: usize) -> String {
    let path = host
        .reviews
        .join(format!("review-{}-{round}.prompt.txt", &head[..8]));
    fs::read_to_string(path).unwrap()
}

const STATUS_LINE: &str = "SKIPPED plangraph:STATUS-ONLY Waiting for a fixture.";
const SLICES_CONTENT: &str = r#"{"items":[{"id":"DEFERRED-SLICE"}]}"#;

pub(super) fn write_writer_artifacts(host: &Host) {
    let status = host.reviews.join("status.txt");
    let slices = host.reviews.join("slices.json");
    fs::write(&status, format!("{STATUS_LINE}\n")).unwrap();
    fs::write(&slices, SLICES_CONTENT).unwrap();
    fs::write(
        &host.record_path,
        json!({"writer_status_path":status,"writer_slices_path":slices}).to_string(),
    )
    .unwrap();
}

pub(super) fn assert_writer_artifacts(host: &Host, head: &str) {
    let prompt = saved_prompt(host, head, 2);
    assert!(prompt.contains(&format!("> {STATUS_LINE}")));
    assert!(prompt.contains(&format!("> {SLICES_CONTENT}")));
}

pub(super) fn assert_foreign_trailer_refused(host: &mut Host, git: impl Fn(&[&str]) -> String) {
    host.brief
        .push_str("\nWhy: depends on plangraph:UNASSIGNED\n");
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
    host.git(&[
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "--allow-empty",
        "-qm",
        "fixture",
    ])
    .unwrap();
    let head = host.git(&["rev-parse", "HEAD"]).unwrap();
    let base = head.clone();
    let prompt = host
        .review_prompt(
            &base,
            &head,
            None,
            &["openai".to_owned()],
            "VERSIONED BRIEF",
            "A nonempty review skill.",
        )
        .unwrap();
    assert!(prompt.starts_with(TOOLS_ONLY_OPEN));
    let open = 0;
    let close = prompt.find(TOOLS_ONLY_CLOSE).unwrap();
    let block = &prompt[open..close];
    assert!(
        block.contains("run read-only commands and the tests"),
        "{block}"
    );
    // Nothing outside the block asks for a tool run or a file read.
    assert!(
        prompt.find("Writer brief").unwrap() > close,
        "the block precedes the writer brief"
    );
    let outside = format!("{}{}", &prompt[..open], &prompt[close..]);
    assert!(
        !outside.contains("run read-only commands") && !outside.contains("Open callers"),
        "{outside}"
    );
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
