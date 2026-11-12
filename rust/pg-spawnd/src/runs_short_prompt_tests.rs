use super::*;

#[test]
fn short_secret_embedded_in_argv_and_env_is_redacted() {
    let dir = std::env::temp_dir().join(new_run_id());
    for raw in ["sk-learn", "TOKEN=abc"] {
        assert!(raw.len() < 16);
        let clean = pg_worker_core::redact::redact(raw);
        assert_ne!(clean, raw);
        let mut launch = crate::launch::LaunchSpec {
            argv: vec!["agy".into(), format!("prefix {raw} suffix")],
            cwd: dir.clone(),
            env: BTreeMap::from([("OTHER".into(), format!("prefix {raw} suffix"))]),
            stdin: Some(raw.into()),
            prompt: Some(raw.into()),
            warnings: None,
            events: None,
        };
        let id = new_run_id();
        assert!(store_prompt(&dir, &id, &mut launch, &mut json!({}), false).is_ok());
        assert_eq!(launch.argv[1], format!("prefix {clean} suffix"));
        assert_eq!(launch.env["OTHER"], format!("prefix {clean} suffix"));
        assert_eq!(launch.stdin.as_deref(), Some(clean.as_str()));
        assert_eq!(
            std::fs::read_to_string(dir.join("runs").join(id).join("prompt.txt")).unwrap(),
            clean
        );
    }
    std::fs::remove_dir_all(dir).unwrap();
}
