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
