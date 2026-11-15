use super::*;
use serde_json::json;
fn launch(prompt: &str) -> LaunchSpec {
    LaunchSpec {
        argv: vec!["native".into()],
        cwd: ".".into(),
        env: BTreeMap::new(),
        stdin: Some(prompt.into()),
        prompt: Some(prompt.into()),
        warnings: None,
        events: None,
    }
}
#[test]
fn inline_only_missing_names_diff_ref_before_record_child() {
    let mut carrier = launch("one\ntwo");
    carrier.argv[0] = "runner.cmd".into();
    let error = prepare_review(
        &json!({"role":"review"}),
        &json!({"review_mode":"inline_only"}),
        &mut carrier,
        &BTreeMap::new(),
        false,
    )
    .unwrap_err();
    assert_eq!(error, InlineDiffError::MissingDiffRef);
    assert!(error.to_string().contains("diff_ref"));
}
#[test]
fn no_diff_ref_tools_unchanged() {
    let mut l = launch("context");
    prepare_review(
        &json!({"role":"review"}),
        &json!({}),
        &mut l,
        &BTreeMap::new(),
        true,
    )
    .unwrap();
    assert_eq!(l.prompt.as_deref(), Some("context"));
}
#[test]
fn tools_without_prompt_unchanged_unless_injecting() {
    for (reference, enabled) in [(json!(null), true), (json!({}), false)] {
        let mut l = launch("context");
        l.prompt = None;
        prepare_review(
            &json!({"role":"review", "card":{"diff_ref":reference}}),
            &json!({}),
            &mut l,
            &BTreeMap::new(),
            enabled,
        )
        .unwrap();
        assert!(l.prompt.is_none());
        assert_eq!(l.stdin.as_deref(), Some("context"));
    }
}
#[test]
fn tools_off_shadow_unchanged() {
    let mut l = launch("context");
    prepare_review(
        &json!({"role":"review", "card":{"diff_ref":{}}}),
        &json!({}),
        &mut l,
        &BTreeMap::new(),
        false,
    )
    .unwrap();
    assert_eq!(l.stdin.as_deref(), Some("context"));
}
#[test]
fn windows_cmd_multiline_refused_case_insensitive() {
    for exe in ["runner.CmD", "runner.BAT"] {
        let mut l = launch("one\ntwo");
        l.argv = vec!["cmd.exe".into(), "/c".into(), exe.into()];
        assert_eq!(
            prepare_review(
                &json!({"role":"review"}),
                &json!({}),
                &mut l,
                &BTreeMap::new(),
                false
            ),
            Err(InlineDiffError::UnsafeCarrier)
        );
    }
}
#[test]
fn switch_precedence_and_role() {
    let config =
        json!({"inline_review_diff":"shadow", "roles":{"review":{"inline_review_diff":"on"}}});
    assert_eq!(mode(None, &config, "review"), "on");
    assert_eq!(mode(None, &config, "implement"), "shadow");
    assert_eq!(mode(Some("off"), &config, "review"), "off");
    assert_eq!(mode(Some("invalid"), &config, "review"), "off");
}
#[test]
fn non_review_without_prompt_unchanged() {
    let mut l = launch("one\ntwo");
    l.prompt = None;
    l.argv[0] = "runner.cmd".into();
    prepare_review(
        &json!({"role":"implement"}),
        &json!({}),
        &mut l,
        &BTreeMap::new(),
        true,
    )
    .unwrap();
    assert!(l.prompt.is_none());
}
#[test]
fn windows_launcher_inspection_native_child() {
    for exe in ["native.exe", "native"] {
        let mut l = launch("λ\r\nsecond\n");
        l.argv[0] = exe.into();
        assert_eq!(windows_guard(&l, l.prompt.as_ref().unwrap()), Ok(()));
    }
}

#[test]
fn shared_preparation_keeps_inline_only_reference_validation() {
    let mut carrier = launch("review");
    let args = json!({"role":"review", "card":{"diff_ref":{
        "repo":"/caller-selected-checkout", "base":"HEAD", "head":"HEAD"
    }}});
    let error = prepare_launch(
        args.as_object().unwrap(),
        &json!({"harness":"free", "review_mode":"inline_only"}),
        &mut carrier,
        false,
    )
    .unwrap_err()
    .into_error_data();
    assert_eq!(error.message, "invalid diff_ref");
    assert_eq!(carrier.prompt.as_deref(), Some("review"));
    assert_eq!(carrier.stdin.as_deref(), Some("review"));
}

#[test]
fn writer_status_without_self_check_answers_blocks_review() {
    let mut carrier = launch("review");
    let args = json!({"role":"review", "card":{
        "done_criteria":"Missing answers block review", "writer_status":"Tests passed"
    }});
    assert!(prepare_launch(args.as_object().unwrap(), &json!({}), &mut carrier, false).is_err());
}

#[test]
fn generated_writer_checklist_is_answered_and_forwarded_to_reviewer() {
    let dir = tempfile::tempdir().unwrap();
    std::fs::write(
        dir.path().join("recent.txt"),
        "HIGH: missing regression test\nMED: docs disagree with code\nVERDICT: REQUEST_CHANGES\n",
    )
    .unwrap();
    std::fs::write(
        dir.path().join("summary.txt"),
        "VERDICT: ACCEPT\nNo HIGH/MED contract findings.\n",
    )
    .unwrap();
    let old = dir.path().join("old.txt");
    std::fs::write(
        &old,
        "HIGH: Python Rust contract mismatch\nVERDICT: REQUEST_CHANGES\n",
    )
    .unwrap();
    // Windows requires a write handle for set_times; read-only File::open fails.
    std::fs::OpenOptions::new()
        .write(true)
        .open(&old)
        .unwrap()
        .set_times(std::fs::FileTimes::new().set_modified(
            std::time::SystemTime::now() - std::time::Duration::from_secs(31 * 86400),
        ))
        .unwrap();
    assert!(
        old.metadata()
            .unwrap()
            .modified()
            .unwrap()
            .elapsed()
            .unwrap()
            .as_secs()
            > 30 * 86400
    );
    let mut writer = launch("item body");
    let mut args =
        json!({"card":{"done_criteria":"Review blocks missing answers", "review_dir":dir.path()}});
    assert!(prepare_launch(args.as_object().unwrap(), &json!({}), &mut writer, false).is_ok());
    let prefix = writer.prompt.as_ref().unwrap();
    assert!(prefix.starts_with("Pre-review self-check"));
    assert!(prefix.contains("Review blocks missing answers"));
    assert!(prefix.contains("regression tests"));
    assert!(prefix.contains("documentation"));
    assert!(prefix.contains("cross-language"));
    assert!(prefix.ends_with("item body"));
    assert_eq!(writer.stdin, writer.prompt);
    let answers = prefix
        .lines()
        .filter(|l| l.starts_with("SELF-CHECK "))
        .map(|l| format!("{}: verified with evidence", l.split_once(':').unwrap().0))
        .collect::<Vec<_>>()
        .join("\n");
    args["role"] = json!("review");
    for status in [
        String::new(),
        answers.replacen("verified with evidence", " ", 1),
        format!("{answers}\n{}", answers.lines().next().unwrap()),
    ] {
        args["card"]["writer_status"] = json!(status);
        assert!(prepare_launch(
            args.as_object().unwrap(),
            &json!({}),
            &mut launch("review"),
            false
        )
        .is_err());
    }
    args["card"]["writer_status"] = json!(answers);
    let mut reviewer = launch("review");
    // Claude's JSON prompt transport must receive the same answers.
    reviewer.stdin = Some(json!({"event":"user", "message":{"content":"review"}}).to_string());
    assert!(prepare_launch(args.as_object().unwrap(), &json!({}), &mut reviewer, false).is_ok());
    assert!(reviewer.prompt.as_ref().unwrap().contains(&answers));
    let event: Value = serde_json::from_str(reviewer.stdin.as_ref().unwrap()).unwrap();
    assert_eq!(
        event["message"]["content"],
        *reviewer.prompt.as_ref().unwrap()
    );
    // A verdict landing between writer launch and review must not change the ids.
    std::fs::write(
        dir.path().join("late.txt"),
        "FINDING: HIGH x - lost data
FINDING: HIGH y - lost data
VERDICT: REQUEST_CHANGES
",
    )
    .unwrap();
    assert!(prepare_launch(
        args.as_object().unwrap(),
        &json!({}),
        &mut launch("r"),
        false
    )
    .is_ok());
    args["card"]["writer_status"] = json!(answers.lines().skip(1).collect::<Vec<_>>().join(
        "
"
    ));
    let err = prepare_launch(
        args.as_object().unwrap(),
        &json!({}),
        &mut launch("r"),
        false,
    )
    .err()
    .unwrap()
    .into_error_data();
    assert!(err.message.contains("Verify every done criterion"));
    args["card"]["writer_status"] = json!(answers);
    args["card"]["done_criteria"] = json!("Different criteria");
    assert!(prepare_launch(
        args.as_object().unwrap(),
        &json!({}),
        &mut launch("review"),
        false
    )
    .is_err());
}

#[test]
fn checklist_lists_all_classes_by_count_and_refuses_unreadable_history() {
    let dir = tempfile::tempdir().unwrap();
    std::fs::write(dir.path().join("findings.md"), "VERDICT: REQUEST_CHANGES\nHIGH: missing tests\nMED: docs wrong\nMED: contract mismatch\nHIGH: lost data\nHIGH: lost data again\n").unwrap();
    std::fs::write(
        dir.path().join("not-a-verdict.txt"),
        "HIGH: docs\nHIGH: docs\nHIGH: docs\n",
    )
    .unwrap();
    let mut carrier = launch("writer");
    let args = json!({"card":{"done_criteria":"done", "review_dir":dir.path()}});
    assert!(prepare_launch(args.as_object().unwrap(), &json!({}), &mut carrier, false).is_ok());
    let text = carrier.prompt.unwrap();
    assert_eq!(
        text.lines()
            .filter(|l| l.starts_with("SELF-CHECK "))
            .count(),
        5
    );
    assert!(text.find("other recent").unwrap() < text.find("regression tests").unwrap());
    assert!(text.contains("documentation") && text.contains("cross-language"));
    let args =
        json!({"card":{"done_criteria":"done", "review_dir":dir.path().join("findings.md")}});
    assert!(prepare_launch(
        args.as_object().unwrap(),
        &json!({}),
        &mut launch("writer"),
        false
    )
    .is_err());
}

#[test]
fn self_check_rejects_mismatched_or_exposed_prompt_transport() {
    let args = json!({"card":{"done_criteria":"done"}});
    for variant in 0..3 {
        let mut carrier = launch("writer task");
        match variant {
            0 => carrier.argv.push("writer task".into()),
            1 => {
                carrier.env.insert("TASK".into(), "writer task".into());
            }
            _ => carrier.stdin = Some("different task".into()),
        }
        assert!(
            prepare_launch(args.as_object().unwrap(), &json!({}), &mut carrier, false).is_err()
        );
        assert_eq!(carrier.prompt.as_deref(), Some("writer task"));
    }
}

#[test]
fn finding_lines_in_review_seat_formats_are_counted_by_description() {
    use crate::pre_review::finding_description as d;
    assert_eq!(d("HIGH: missing test"), Some(": missing test"));
    assert_eq!(d("  - **MED** docs wrong"), Some("** docs wrong"));
    assert_eq!(d("1. **HIGH** lost data"), Some("** lost data"));
    assert_eq!(d("FINDING: MED rust/a.rs:1 - docs"), Some("docs"));
    assert_eq!(d("FINDING: HIGH tests/x.py - lost"), Some("lost"));
    for line in [
        "FINDING: LOW a - b",
        "No HIGH findings",
        "1 HIGH",
        "MEDIUM x",
    ] {
        assert_eq!(d(line), None, "{line}");
    }
}
