use super::*;

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
    let (base, head) = ("b".repeat(40), "c".repeat(40));
    let prompt = host.review_prompt(
        &base,
        &head,
        None,
        &["openai".to_owned()],
        "VERSIONED BRIEF",
        "A nonempty review skill.",
    );
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
