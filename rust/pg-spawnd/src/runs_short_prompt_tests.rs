use super::*;

#[test]
fn short_prompt_in_metadata_refuses_before_recording() {
    let dir = std::env::temp_dir().join(new_run_id());
    for (raw, binary) in [
        ("tiny task", "opencode"),
        ("x", "/bin/opencode"),
        ("é", "opencode.exe"),
        ("sk-learn", "opencode"),
    ] {
        for flag in ["--model", "--title"] {
            for inline in [false, true] {
                let value = format!("prefix {raw} suffix");
                let mut argv = vec![binary.into(), "run".into()];
                argv.extend(if inline {
                    vec![format!("{flag}={value}")]
                } else {
                    vec![flag.into(), value]
                });
                let mut launch = crate::launch::LaunchSpec {
                    argv,
                    cwd: dir.clone(),
                    env: BTreeMap::new(),
                    stdin: Some(raw.into()),
                    prompt: Some(raw.into()),
                    warnings: None,
                    events: None,
                };
                let id = new_run_id();
                assert!(store_prompt(&dir, &id, &mut launch, &mut json!({}), false).is_err());
                assert!(!dir.join("runs").join(id).join("prompt.txt").exists());
            }
        }
    }
    assert!(!dir.exists());
}

#[test]
fn generated_title_prefix_is_not_a_prompt_carrier() {
    for raw in ["p", "plangraph", "json"] {
        let launch = crate::launch::LaunchSpec {
            argv: vec![
                "opencode".into(),
                "run".into(),
                "--title".into(),
                "plangraph-job-12345678".into(),
                "--format".into(),
                "json".into(),
            ],
            cwd: std::env::temp_dir(),
            env: BTreeMap::new(),
            stdin: Some(raw.into()),
            prompt: Some(raw.into()),
            warnings: None,
            events: None,
        };
        // Exact argv values still refuse; only the generated prefix is incidental.
        assert_eq!(crate::launch::prompt_carrier(&launch, raw), raw == "json");
    }
}

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
