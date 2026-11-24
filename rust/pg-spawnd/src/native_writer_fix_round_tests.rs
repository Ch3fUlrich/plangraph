use super::*;

#[test]
fn native_fix_round_groups_findings_on_resume_and_relaunch() {
    let mut driver = Fake {
        verdicts: vec![("REQUEST_CHANGES", 0)],
        first_review_text: Some(
            "HIGH src/a.rs:4 - first defect\nMED cross-cutting defect\nMED src/a.rs:8 - second defect\nVERDICT: REQUEST_CHANGES\n",
        ),
        fail_turn: Some(2),
        ..fake()
    };
    run(&mut driver, json!({}), "brief");
    assert_eq!(driver.calls, vec![None, Some("same-session".into()), None]);
    for prompt in &driver.prompts[1..] {
        assert_eq!(prompt.matches("### src/a.rs").count(), 1, "{prompt}");
        assert!(prompt.contains("### Findings without a file"), "{prompt}");
        for finding in ["first defect", "second defect", "cross-cutting defect"] {
            assert_eq!(prompt.matches(finding).count(), 1, "{prompt}");
        }
        assert_eq!(prompt.matches("Class-Search:").count(), 3, "{prompt}");
    }
}
