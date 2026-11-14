use pg_worker_core::{host_launch::classify, host_overflow::Launch};
use serde_json::json;
#[test]
fn unsafe_ssh_route_is_refused_before_launch() {
    let registry = pg_worker_core::hosts::HostRegistry {
        version: 1,
        self_host: "local".into(),
        hosts: vec![],
    };
    for destination in [
        "worker;evil",
        "user@-worker",
        "$(evil)",
        "",
        "worker\nother",
    ] {
        let candidate = pg_worker_core::package_placement::Candidate {
            id: "worker".into(),
            workstation: false,
            ssh: Some(destination.into()),
            sample: None,
        };
        assert!(matches!(
            pg_worker_core::host_launch::launch(&registry, &candidate, &json!({})),
            Launch::Refused(reason) if reason == "invalid SSH route"
        ));
    }
}
#[test]
fn receipts_require_matching_host_and_explicit_refusals() {
    assert!(matches!(
        classify(
            &json!({"result":{"content":[{"text":"{\"host\":\"worker\",\"status\":\"running\",\"run_id\":\"run-abc\"}"}]}}),
            "worker"
        ),
        Launch::Started(_)
    ));
    assert!(matches!(
        classify(
            &json!({"error":{"data":{"type":"SpawnRefused"},"message":"load gate"}}),
            "worker"
        ),
        Launch::Refused(_)
    ));
    for value in [
        json!({}),
        json!({"error":{"message":"connection lost"}}),
        json!({"result":{"content":[{"text":"{\"host\":\"other\",\"status\":\"running\",\"run_id\":\"run-abc\"}"}]}}),
    ] {
        assert!(matches!(classify(&value, "worker"), Launch::Uncertain(_)));
    }
}
#[test]
#[cfg(unix)]
fn real_stdio_handshake_launches_without_model_or_live_host() {
    let tmp = tempfile::tempdir().unwrap();
    let script = tmp.path().join("fixture.sh");
    let reply = json!({"id":2,"result":{"content":[{"text":"{\"host\":\"worker\",\"status\":\"running\",\"run_id\":\"run-abc\"}"}]}});
    std::fs::write(&script, format!("read init\nprintf '%s\\n' '{{\"id\":1,\"result\":{{}}}}'\nread initialized\nread spawn\nprintf '%s\\n' '{reply}'\n")).unwrap();
    let mut cmd = std::process::Command::new("sh");
    cmd.arg(&script).current_dir(tmp.path());
    let result = pg_worker_core::host_launch::launch_using(
        &mut cmd,
        "worker",
        &json!({"card":{},"prompt":"fixture"}),
    );
    assert!(matches!(result, Launch::Started(_)));
}
