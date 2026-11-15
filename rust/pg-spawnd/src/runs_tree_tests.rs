use super::*;
use pg_worker_core::run::{group::Confinement, supervise::Config, supervisor::SupervisorCfg};

#[test]
fn durable_records_copy_tree_identity_or_explain_its_absence() {
    for dry in [false, true] {
        for event in [
            Value::Null,
            json!({"event":"run_tree","tree_sha":"a".repeat(40),"tree_path":format!("/fixture/trees/{}", "a".repeat(40)),"tree_stale":"fetch_failed"}),
            json!({"event":"run_tree","tree_absent":"installed adapter without checkout"}),
            json!({"event":"run_tree","tree_sha":"invalid"}),
        ] {
            let dir = tempfile::tempdir().unwrap();
            let runs = Runs::new(Arc::new(
                Supervisor::new(SupervisorCfg::new(Config {
                    state_dir: dir.path().into(),
                    confinement: Confinement::Plain,
                    ..Config::default()
                }))
                .unwrap(),
            ));
            let args = json!({"prompt":"p","card":{},"dry_run":dry,"wait_seconds":5});
            let result = runs
                .spawn_using(
                    dir.path(),
                    args.as_object().unwrap(),
                    "fixture",
                    json!("review"),
                    json!({"first_leg":{"family":"fixture"}}),
                    |_, _| {
                        Ok(crate::launch::LaunchSpec {
                            argv: vec!["/bin/cat".into()],
                            cwd: dir.path().into(),
                            env: BTreeMap::new(),
                            stdin: Some("p".into()),
                            prompt: Some("p".into()),
                            warnings: None,
                            events: event.as_object().map(|e| vec![e.clone()]),
                        })
                    },
                )
                .unwrap_or_else(|_| panic!("spawn failed"));
            let saved = crate::records::read_record(dir.path(), &result["run_id"])
                .unwrap_or_else(|_| panic!("record missing"));
            if event["tree_sha"] == "a".repeat(40) {
                assert_eq!(saved["tree_sha"], event["tree_sha"], "{saved}");
                assert_eq!(saved["tree_path"], event["tree_path"]);
                assert_eq!(saved["tree_stale"], "fetch_failed");
                assert!(saved.get("tree_absent").is_none());
            } else {
                assert!(saved["tree_sha"].is_null());
                assert!(!saved["tree_absent"].as_str().unwrap().is_empty());
                if event.get("tree_absent").is_some() {
                    assert_eq!(saved["tree_absent"], event["tree_absent"]);
                }
            }
        }
    }
}
