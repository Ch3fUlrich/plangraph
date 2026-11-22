use super::*;

#[test]
fn group_prompt_names_its_commits_and_a_plain_prompt_does_not() {
    let group = WriterGroup {
        families: vec!["google".into()],
        commits: vec!["c1".into(), "c2".into()],
    };
    let named = build_base_prompt("base", "head", None, None, Some(&group));
    assert!(
        named.contains("commits written by google: c1 c2"),
        "{named}"
    );
    let plain = build_base_prompt("base", "head", None, None, None);
    assert!(!plain.contains("commits written by"), "{plain}");
}
