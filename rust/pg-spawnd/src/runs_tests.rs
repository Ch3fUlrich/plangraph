use super::*;

#[test]
fn gitconfig_guard_only_arms_writers() {
    for role in [
        json!("review"),
        json!("research"),
        json!("implement"),
        json!("orchestrate"),
        json!("session"),
        json!("judge"),
        json!("unknown"),
        Value::Null,
        json!({"malformed": true}),
    ] {
        let dir = Dir(std::env::temp_dir().join(new_run_id()));
        std::fs::create_dir_all(dir.0.join(".git")).unwrap();
        let config = dir.0.join(".git/config");
        std::fs::write(&config, b"original\n").unwrap();
        let runs = Runs::new(Arc::new(
            Supervisor::new(SupervisorCfg::new(Config {
                state_dir: dir.0.clone(),
                confinement: Confinement::Plain,
                ..Config::default()
            }))
            .unwrap(),
        ));
        let args = json!({"prompt":"unique-input","card":{},"wait_seconds":5});
        let result = runs
            .spawn_using(
                &dir.0,
                args.as_object().unwrap(),
                "local",
                json!(role),
                json!({"first_leg":{"family":"f"}}),
                |_, _| {
                    Ok(crate::launch::LaunchSpec {
                        argv: vec![
                            "/bin/sh".into(),
                            "-c".into(),
                            "cat >/dev/null; printf 'changed\\n' >.git/config".into(),
                        ],
                        cwd: dir.0.clone(),
                        env: BTreeMap::new(),
                        stdin: Some("unique-input".into()),
                        prompt: Some("unique-input".into()),
                        warnings: None,
                        events: None,
                    })
                },
            )
            .unwrap_or_else(|_| panic!("spawn failed"));
        let writer = role != "review" && role != "research";
        assert_eq!(
            result.get("gitconfig_guard"),
            writer.then_some(&json!(true))
        );
        let run_dir =
            crate::gitconfig_guard::run_dir(&dir.0, result["run_id"].as_str().unwrap()).unwrap();
        assert_eq!(run_dir.join("gitconfig.snapshot").exists(), writer);
        assert_eq!(
            result["status"],
            if writer { "failed" } else { "completed" }
        );
        // The failed verdict is persisted before restoration; wait for the hook.
        let deadline = Instant::now() + Duration::from_secs(5);
        while writer && std::fs::read(&config).unwrap() != b"original\n" {
            assert!(
                Instant::now() < deadline,
                "completion did not restore config"
            );
            std::thread::sleep(Duration::from_millis(10));
        }
        assert_eq!(
            std::fs::read(&config).unwrap(),
            if writer {
                b"original\n".as_slice()
            } else {
                b"changed\n".as_slice()
            }
        );
        std::fs::write(&config, b"restart mutation\n").unwrap();
        let mut record = RunRecord::read(&dir.0, result["run_id"].as_str().unwrap()).unwrap();
        assert_eq!(
            crate::gitconfig_guard::enforce(&mut record, Status::Orphaned, json!({})),
            if writer {
                Status::Failed
            } else {
                Status::Orphaned
            }
        );
        assert_eq!(
            std::fs::read(&config).unwrap(),
            if writer {
                b"original\n".as_slice()
            } else {
                b"restart mutation\n".as_slice()
            }
        );
        // Recovery calls the same finish hook as completion, with an on-disk record.
        std::fs::write(&config, b"recovery mutation\n").unwrap();
        let mut record = RunRecord::read(&dir.0, result["run_id"].as_str().unwrap()).unwrap();
        finish(&mut record, Status::Orphaned, &json!({}), 1);
        assert_eq!(
            std::fs::read(&config).unwrap(),
            if writer {
                b"original\n".as_slice()
            } else {
                b"recovery mutation\n".as_slice()
            }
        );
        assert_eq!(record.get("gitconfig_original_outcome").is_some(), writer);
    }
}

#[test]
fn embedded_prompt_and_distinct_stdin() {
    let dir = std::env::temp_dir().join(new_run_id());
    let raw = format!("{}{}", "sk-", "a".repeat(24));
    for stdin in ["other".to_owned(), format!("prefix {raw} suffix")] {
        let mut launch = crate::launch::LaunchSpec {
            argv: vec!["sh".into(), "-c".into(), format!("echo {raw}")],
            cwd: dir.clone(),
            env: BTreeMap::new(),
            stdin: Some(stdin.clone()),
            prompt: Some(raw.clone()),
            warnings: None,
            events: None,
        };
        let id = new_run_id();
        if stdin.contains(&raw) {
            assert!(store_prompt(&dir, &id, &mut launch, &mut json!({}), false).is_err());
            assert!(!dir.join("runs").join(id).join("prompt.txt").exists());
            continue;
        }
        assert!(store_prompt(&dir, &id, &mut launch, &mut json!({}), false).is_ok());
        assert_eq!(launch.argv[2], "echo [REDACTED]");
        assert_eq!(launch.stdin.unwrap(), stdin.replace(&raw, "[REDACTED]"));
        assert_eq!(
            std::fs::read(dir.join("runs").join(id).join("prompt.txt")).unwrap(),
            if stdin.contains(&raw) {
                stdin.replace(&raw, "[REDACTED]")
            } else {
                "echo [REDACTED]".into()
            }
            .as_bytes()
        );
    }
    std::fs::remove_dir_all(dir).unwrap();
}

#[test]
fn prompt_snapshot_before_dispatch() {
    use std::os::unix::fs::PermissionsExt;
    let dir = std::env::temp_dir().join(new_run_id());
    let id = new_run_id();
    let raw = format!("héllo {}{}", "sk-", "a".repeat(24));
    let mut launch = crate::launch::LaunchSpec {
        argv: vec!["fake".into(), raw.clone()],
        cwd: dir.clone(),
        env: BTreeMap::new(),
        stdin: Some(raw.clone()),
        prompt: Some(raw.clone()),
        warnings: None,
        events: None,
    };
    let mut base = json!({});
    assert!(store_prompt(&dir, &id, &mut launch, &mut base, false).is_ok());
    let path = dir.join("runs").join(&id).join("prompt.txt");
    let bytes = std::fs::read(&path).unwrap();
    assert_eq!(bytes, launch.stdin.as_ref().unwrap().as_bytes());
    assert_eq!(bytes, launch.argv[1].as_bytes());
    assert_eq!(bytes, "héllo [REDACTED]".as_bytes());
    assert!(!base.to_string().contains(&raw));
    use sha2::{Digest, Sha256};
    assert_eq!(
        base["prompt_sha256"],
        format!("{:x}", Sha256::digest(&bytes))
    );
    assert_eq!(base["wake_variant"], "initial");
    assert_eq!(
        std::fs::metadata(&path).unwrap().permissions().mode() & 0o777,
        0o600
    );
    let old = std::fs::File::open(&path).unwrap();
    launch.prompt = Some("replacement".into());
    launch.stdin = Some("replacement".into());
    assert!(store_prompt(&dir, &id, &mut launch, &mut base, false).is_ok());
    use std::io::Read;
    let mut previous = Vec::new();
    (&old).read_to_end(&mut previous).unwrap();
    assert_eq!(previous, bytes); // Replacement must not truncate the old inode.
    assert_eq!(std::fs::read(&path).unwrap(), b"replacement");
    std::fs::remove_file(&path).unwrap();
    std::fs::create_dir(&path).unwrap();
    assert!(store_prompt(&dir, &id, &mut launch, &mut base, false).is_err());
    launch.prompt = None;
    assert!(store_prompt(&dir, &id, &mut launch, &mut base, false).is_err());
    std::fs::remove_dir_all(dir).unwrap();
}
use pg_worker_core::run::{group::Confinement, supervise::Config, supervisor::SupervisorCfg};

struct Dir(std::path::PathBuf);
impl Drop for Dir {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

#[test]
fn real_spawn_registers_job() {
    registered_job(false);
}

#[test]
fn dry_spawn_registers_job() {
    registered_job(true);
}

#[test]
fn card_max_s_is_ignored_without_persisting_a_kill_budget() {
    for value in [Value::Null, json!(1800.0), json!("bad")] {
        for dry in [false, true] {
            registered_job_with_card(dry, json!({"max_s":value,"max_min":120}));
        }
    }
}

fn registered_job(dry: bool) {
    registered_job_with_card(dry, json!({}));
}

fn registered_job_with_card(dry: bool, card: Value) {
    let dir = Dir(std::env::temp_dir().join(new_run_id()));
    std::fs::create_dir(&dir.0).unwrap();
    let runs = Runs::new(Arc::new(
        Supervisor::new(SupervisorCfg::new(Config {
            state_dir: dir.0.clone(),
            confinement: Confinement::Plain,
            ..Config::default()
        }))
        .unwrap(),
    ));
    let args = json!({"prompt":"p","card":card,"wait_seconds":1,"dry_run":dry});
    let result = runs
        .spawn_using(
            &dir.0,
            args.as_object().unwrap(),
            "local",
            json!("reader"),
            json!({"first_leg":{"family":"f"}}),
            |_, _| {
                Ok(crate::launch::LaunchSpec {
                    argv: vec!["/bin/cat".into()],
                    cwd: dir.0.clone(),
                    env: BTreeMap::new(),
                    stdin: Some("p".into()),
                    prompt: Some("p".into()),
                    warnings: None,
                    events: None,
                })
            },
        )
        .unwrap_or_else(|_| panic!("spawn failed"));
    assert_eq!(
        runs.jobs
            .lock()
            .unwrap()
            .get(result["job_id"].as_str().unwrap()),
        Some(&result["run_id"].as_str().unwrap().to_string())
    );
    assert!(terminal(&result));
    let saved = crate::records::read_record(&dir.0, &result["run_id"])
        .unwrap_or_else(|_| panic!("missing record"));
    assert!(saved.get("max_s").is_none());
    if !dry {
        let path = dir
            .0
            .join("runs")
            .join(result["run_id"].as_str().unwrap())
            .join("prompt.txt");
        assert_eq!(
            std::fs::read(path).unwrap(),
            result["stdout_tail"].as_str().unwrap().as_bytes()
        );
    }
    assert_eq!(result["wake_variant"], "initial");
    use sha2::{Digest, Sha256};
    assert_eq!(
        result["prompt_sha256"],
        format!("{:x}", Sha256::digest(b"p"))
    );
    if dry {
        assert!(!dir
            .0
            .join("runs")
            .join(result["run_id"].as_str().unwrap())
            .join("prompt.txt")
            .exists());
        let id = runs.jobs.lock().unwrap()[result["job_id"].as_str().unwrap()].clone();
        let saved = crate::records::read_record(&dir.0, &json!(id))
            .unwrap_or_else(|_| panic!("missing dry record"));
        assert_eq!(saved["status"], "dry_run");
    }
}

#[test]
fn env_prompt_and_missing_carrier() {
    for (text, expected) in [
        (
            "risk-tagged task-contract sk-learn".to_owned(),
            "risk-tagged task-contract [REDACTED]",
        ),
        ("xsk-".to_owned() + &"a".repeat(20), "[REDACTED]"),
    ] {
        assert_eq!(pg_worker_core::redact::redact(&text), expected);
    }
    let dir = Dir(std::env::temp_dir().join(new_run_id()));
    let raw = ["sk-", &"a".repeat(24)].concat();
    let mut launch = crate::launch::LaunchSpec {
        argv: vec!["sh".into(), "-c".into(), "printf %s \"$PROMPT\"".into()],
        cwd: dir.0.clone(),
        env: BTreeMap::from([("PROMPT".into(), raw.clone())]),
        stdin: None,
        prompt: Some(raw.clone()),
        warnings: None,
        events: None,
    };
    let id = new_run_id();
    let mut base = json!({});
    assert!(store_prompt(&dir.0, &id, &mut launch, &mut base, false).is_ok());
    let child = std::process::Command::new(&launch.argv[0])
        .args(&launch.argv[1..])
        .envs(&launch.env)
        .output()
        .unwrap();
    assert_eq!(child.stdout, b"[REDACTED]");
    assert_eq!(
        std::fs::read(dir.0.join("runs").join(id).join("prompt.txt")).unwrap(),
        child.stdout
    );
    assert!(!base.to_string().contains(&raw));
    launch.prompt = Some("absent".into());
    assert!(store_prompt(&dir.0, &new_run_id(), &mut launch, &mut base, false).is_err());
}

#[test]
fn negative_wait_is_nonblocking() {
    let value = json!(-1);
    assert_eq!(wait_duration(Some(&value)), Duration::ZERO);
}
