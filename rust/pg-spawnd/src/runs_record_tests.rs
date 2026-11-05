use super::*;

#[test]
fn empty_prompt_and_metadata() {
    let dir = std::env::temp_dir().join(new_run_id());
    let secret = format!("sk-{}", "a".repeat(24));
    let mut launch = crate::launch::LaunchSpec {
        argv: vec!["fake".into()],
        cwd: dir.clone(),
        env: BTreeMap::new(),
        stdin: None,
        prompt: Some(String::new()),
        warnings: Some(vec![format!("warning {secret}")]),
        events: Some(vec![json!({"message":secret}).as_object().unwrap().clone()]),
    };
    let id = new_run_id();
    assert!(store_prompt(&dir, &id, &mut launch, &mut json!({}), false).is_ok());
    assert_eq!(launch.argv, ["fake"]);
    assert_eq!(launch.prompt.as_deref(), Some(""));
    assert_eq!(
        std::fs::read(dir.join("runs").join(id).join("prompt.txt")).unwrap(),
        b""
    );
    assert!(!serde_json::to_string(&launch.warnings)
        .unwrap()
        .contains(&secret));
    assert!(!serde_json::to_string(&launch.events)
        .unwrap()
        .contains(&secret));
    std::fs::remove_dir_all(dir).unwrap();
}
