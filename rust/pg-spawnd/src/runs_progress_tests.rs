use super::*;

#[test]
fn round_refusal_is_saved_before_launch_and_does_not_consume_a_round() {
    use pg_worker_core::run::{supervise::Config, supervisor::SupervisorCfg};
    let dir = tempfile::tempdir().unwrap();
    let mut cfg = SupervisorCfg::new(Config {
        state_dir: dir.path().to_owned(),
        ..Config::default()
    });
    cfg.max_runs = 1;
    let runs = Runs::new(Arc::new(Supervisor::new(cfg).unwrap()));
    for index in 1..=5 {
        let source = dir.path().join(format!("verdict-{index}.json"));
        std::fs::write(
            &source,
            json!({"writer_run_id":format!("run-r{index}"),
            "item_key":"p:I", "pr":null, "verdict":"REQUEST_CHANGES",
            "findings":[]})
            .to_string(),
        )
        .unwrap();
        create(
            dir.path(),
            &format!("run-r{index}"),
            "fixture",
            json!({"role":"implement", "item_key":"p:I", "writer_round":index, "round_verdict_file":source}),
            Status::Completed,
        )
        .ok()
        .unwrap();
    }
    let args = json!({"card":{"item_key":"p:I"},"prompt":"p"});
    for _ in 0..2 {
        let result = runs
            .spawn_using(
                dir.path(),
                args.as_object().unwrap(),
                "fixture",
                json!("implement"),
                json!({}),
                |_, _| panic!("refused writer launched"),
            )
            .ok()
            .unwrap();
        assert_eq!(result["status"], "refused");
        assert_eq!(result["reason"], "round_cap");
        assert_eq!(result["item_key"], "p:I");
        assert!(result["writer_round"].is_null());
        let saved = crate::records::read_record(dir.path(), &result["run_id"])
            .ok()
            .unwrap();
        assert_eq!(saved["reason"], "round_cap");
    }
}
