use plangraph_cli::codex_readiness::{check, Probe};
use std::collections::BTreeMap;

struct Host(BTreeMap<&'static str, Result<(), String>>);
impl Probe for Host {
    fn check(&mut self, point: &'static str) -> Result<(), String> {
        self.0.remove(point).unwrap_or(Ok(()))
    }
}
#[test]
fn november_13_failures_are_reported_without_starting_a_model() {
    let mut host = Host(BTreeMap::from([
        ("sandbox", Err("bwrap RTM_NEWADDR not permitted".into())),
        ("commit", Err("git metadata is read-only".into())),
    ]));
    let rows = check(&mut host);
    assert_eq!(
        rows,
        [
            "sandbox: bwrap RTM_NEWADDR not permitted",
            "commit: git metadata is read-only"
        ]
    );
}
#[test]
fn prepared_host_is_ready_and_all_points_are_checked() {
    assert_eq!(check(&mut Host(BTreeMap::new())), ["READY"]);
}

use plangraph_cli::codex_readiness::prompt_state;
use serde_json::json;
use std::path::Path;
#[test]
fn folder_hook_and_update_questions_fail_closed() {
    let repo = Path::new("/repo/work");
    let mut config =
        json!({"projects":{"/repo":{"trust_level":"trusted"}},"check_for_update_on_startup":false});
    let mut hooks = json!({"data":[{"errors":[],"warnings":[],"hooks":[{"enabled":true,"trustStatus":"trusted"}]}]});
    assert!(prompt_state(&config, &hooks, repo, None).is_err());
    assert!(prompt_state(&config, &hooks, repo, Some(Path::new("/repo"))).is_ok());
    hooks["data"][0]["hooks"][0]["trustStatus"] = json!("modified");
    assert!(
        prompt_state(&config, &hooks, repo, Some(Path::new("/repo")))
            .unwrap_err()
            .contains("hook trust")
    );
    hooks["data"][0]["hooks"][0]["enabled"] = json!(false);
    assert!(prompt_state(&config, &hooks, repo, Some(Path::new("/repo"))).is_ok());
    config["projects"]["/repo/work"] = json!({"trust_level":"untrusted"});
    config["check_for_update_on_startup"] = json!(true);
    let error = prompt_state(&config, &hooks, repo, Some(Path::new("/repo"))).unwrap_err();
    assert!(error.contains("folder trust") && error.contains("update prompt"));
    assert!(prompt_state(&json!({}), &json!({}), repo, None).is_err());
}
#[test]
fn every_probe_runs_even_after_a_failure_and_output_stays_one_line() {
    struct Recorder(Vec<&'static str>);
    impl Probe for Recorder {
        fn check(&mut self, point: &'static str) -> Result<(), String> {
            self.0.push(point);
            Err("missing\npoint".into())
        }
    }
    let mut host = Recorder(Vec::new());
    let rows = check(&mut host);
    assert_eq!(
        host.0,
        ["version", "login", "prompts", "sandbox", "commit", "memory"]
    );
    assert_eq!(rows.len(), 6);
    assert!(rows.iter().all(|r| !r.contains('\n')));
}
