use super::*;

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
    assert!(store_prompt(&dir, &id, &mut launch, &mut base).is_ok());
    let path = dir.join("runs").join(&id).join("prompt.txt");
    let bytes = std::fs::read(&path).unwrap();
    assert_eq!(bytes, launch.stdin.as_ref().unwrap().as_bytes());
    assert_eq!(bytes, launch.argv[1].as_bytes());
    assert_eq!(bytes, "héllo [REDACTED]".as_bytes());
    assert!(!base.to_string().contains(&raw));
    assert!(base["prompt_sha256"].as_str().unwrap().len() == 64);
    assert_eq!(base["wake_variant"], "initial");
    assert_eq!(
        std::fs::metadata(&path).unwrap().permissions().mode() & 0o777,
        0o600
    );
    std::fs::remove_file(&path).unwrap();
    std::fs::create_dir(&path).unwrap();
    assert!(store_prompt(&dir, &id, &mut launch, &mut base).is_err());
    launch.prompt = None;
    assert!(store_prompt(&dir, &id, &mut launch, &mut base).is_err());
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

fn registered_job(dry: bool) {
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
    let args = json!({"prompt":"p","card":{},"wait_seconds":1,"dry_run":dry});
    let result = runs
        .spawn_using(
            &dir.0,
            args.as_object().unwrap(),
            "local",
            json!("reader"),
            json!({"first_leg":{"family":"f"}}),
            |_, _| {
                Ok(crate::launch::LaunchSpec {
                    argv: vec!["/bin/true".into()],
                    cwd: dir.0.clone(),
                    env: BTreeMap::new(),
                    stdin: None,
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
    if dry {
        let id = runs.jobs.lock().unwrap()[result["job_id"].as_str().unwrap()].clone();
        let saved = crate::records::read_record(&dir.0, &json!(id))
            .unwrap_or_else(|_| panic!("missing dry record"));
        assert_eq!(saved["status"], "dry_run");
    }
}

#[test]
fn negative_wait_is_nonblocking() {
    let value = json!(-1);
    assert_eq!(wait_duration(Some(&value)), Duration::ZERO);
}
