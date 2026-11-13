use super::*;
use serde_json::json;
#[test]
fn package_probe_verdicts_and_progress_deltas() {
    let run = json!({"started":100,"last_progress":100,"snapshot":{"log_size":10,"log_mtime":100,"status_text":"","commits":0}});
    let sample = json!({"alive":true,"agent_status":"working","log_size":10,"log_mtime":100,"status_text":"","commits":0});
    assert_eq!(classify(&run, &sample, 200.0, 600.0)["verdict"], "running");
    assert_eq!(classify(&run, &sample, 800.0, 600.0)["verdict"], "stalled");
    let mut s = sample.clone();
    s["agent_status"] = json!("idle");
    assert_eq!(classify(&run, &s, 200.0, 600.0)["verdict"], "running");
    s["status_text"] = json!("p:X DONE\nALL DONE\n");
    let result = classify(&run, &s, 800.0, 600.0);
    assert_eq!(result["verdict"], "idle-finished");
    assert_eq!(result["counts"]["DONE"], 1);
    s = sample.clone();
    s["agent_status"] = json!("blocked");
    assert_eq!(
        classify(&run, &s, 200.0, 600.0)["verdict"],
        "blocked-prompt"
    );
    s = sample.clone();
    s["alive"] = json!(false);
    assert_eq!(classify(&run, &s, 200.0, 600.0)["verdict"], "dead");
    s = sample.clone();
    s["log_size"] = json!(15);
    let result = classify(&run, &s, 800.0, 600.0);
    assert_eq!(result["log_delta"].as_f64(), Some(5.0));
    assert_eq!(result["idle_seconds"], 0.0);
}
#[test]
fn package_probe_exit_markers_are_anchored_and_case_sensitive() {
    assert_eq!(exit_code("exited 1\nEXIT 0\n"), Some(0));
    for text in [
        "exited 1",
        "prefix EXIT 0",
        "EXIT nope",
        "exit 0",
        "EXIT 0 trailing",
    ] {
        assert_eq!(exit_code(text), None);
    }
    assert_eq!(exit_code("EXIT -1\r\n"), Some(-1));
}
#[test]
fn package_probe_wait_condition_does_not_finish_on_idle_alone() {
    assert!(!ready(
        &json!({"runs":[{"verdict":"running","agent_status":"idle"}]}),
        "any_done"
    ));
    assert!(ready(
        &json!({"runs":[{"verdict":"idle-finished"}]}),
        "any_done"
    ));
    assert!(ready(&json!({"runs":[{"verdict":"stalled"}]}), "stalled"));
}

#[test]
fn package_probe_persists_snapshots_from_fake_host() {
    struct Fake {
        size: u64,
    }
    impl Backend for Fake {
        fn keeper(&mut self, _: &str, _: &str) -> Result<Value, ToolError> {
            panic!("no keeper during probe")
        }
        fn remote(&mut self, _: &Host, script: &str) -> Result<Value, ToolError> {
            assert!(!script.contains("pgrep"));
            Ok(
                json!({"alive":true,"agent_status":"working","log_size":self.size,"log_mtime":123,"status_text":"","commits":1}),
            )
        }
    }
    let state = std::env::temp_dir().join(pg_worker_core::run::record::new_run_id());
    let id = pg_worker_core::run::record::new_run_id();
    let run = json!({"run_id":id,"started":100,"worktree":"/tmp/fixture","log_path":"/tmp/log","status_path":"/tmp/status","pane":"p","agent_name":"fixture"});
    let host = Host {
        kind: "linux".into(),
        target: "fixture".into(),
        repo: "/tmp/fixture".into(),
        root: "/tmp".into(),
    };
    let first =
        one(&state, run, &host, &mut Fake { size: 5 }, 600.0).unwrap_or_else(|_| panic!("probe"));
    let next =
        one(&state, first, &host, &mut Fake { size: 9 }, 600.0).unwrap_or_else(|_| panic!("probe"));
    assert_eq!(next["log_delta"], 4.0);
    assert_eq!(
        package::read(&state, &id).unwrap_or_else(|_| panic!("read"))["snapshot"]["log_size"],
        9
    );
    std::fs::remove_dir_all(state).unwrap();
}
