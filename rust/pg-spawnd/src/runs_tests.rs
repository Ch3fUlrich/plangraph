use super::*;
use pg_worker_core::run::{group::Confinement, supervise::Config, supervisor::SupervisorCfg};

struct Dir(std::path::PathBuf);
impl Drop for Dir {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

#[test]
fn real_spawn_registers_job() {
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
    let args = json!({"prompt":"p","card":{},"wait_seconds":1});
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
}

#[test]
fn negative_wait_is_nonblocking() {
    let value = json!(-1);
    assert_eq!(wait_duration(Some(&value)), Duration::ZERO);
}
