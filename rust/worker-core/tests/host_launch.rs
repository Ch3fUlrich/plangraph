use pg_worker_core::{host_launch::classify, host_overflow::Launch};
use serde_json::json;
#[test]
fn unsafe_ssh_route_is_refused_before_launch() {
    let registry = pg_worker_core::hosts::HostRegistry {
        version: 1,
        public_repos: Default::default(),
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
            readiness: None,
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
    std::fs::write(&script, worker_shim_handshake()).unwrap();
    let mut cmd = std::process::Command::new("sh");
    cmd.arg(&script).current_dir(tmp.path());
    let result = pg_worker_core::host_launch::launch_using(
        &mut cmd,
        "worker",
        &json!({"card":{},"prompt":"fixture"}),
    );
    assert!(matches!(result, Launch::Started(_)));
}

#[cfg(unix)]
fn worker_shim_handshake() -> String {
    let receipt = json!({"host":"worker", "status":"running", "run_id":"run-abc"});
    let reply = json!({"id":2, "result":{"content":[{"text":receipt.to_string()}]}});
    format!(
        "read -r init\nprintf '%s\\n' '{{\"id\":1,\"result\":{{}}}}'\n\
         read -r initialized\nread -r spawn\nprintf '%s\\n' '{reply}'\n"
    )
}

#[cfg(unix)]
fn with_ssh_fixture(test_name: &str, check: impl FnOnce(&std::path::Path)) {
    const FIXTURE: &str = "PLANGRAPH_TEST_WSL_HOST_LAUNCH";
    if let Some(directory) = std::env::var_os(FIXTURE) {
        check(std::path::Path::new(&directory));
        return;
    }

    // Only the child gets a fixture PATH so concurrent tests keep their own environment.
    let tmp = tempfile::tempdir().unwrap();
    pg_worker_core::executable_test_support::write_private_executable(
        &tmp.path().join("ssh"),
        format!(
            "#!/bin/sh\nset -eu\nprintf '%s\\n' \"$@\" > \"${FIXTURE}/ssh-argv\"\n{}",
            worker_shim_handshake()
        ),
    );
    let output = std::process::Command::new(std::env::current_exe().unwrap())
        .args(["--exact", test_name, "--nocapture"])
        .env(FIXTURE, tmp.path())
        .env("PATH", tmp.path())
        .output()
        .unwrap();
    let stdout = String::from_utf8_lossy(&output.stdout);
    assert!(
        output.status.success() && stdout.contains("running 1 test"),
        "{stdout}{}",
        String::from_utf8_lossy(&output.stderr)
    );
}

#[cfg(unix)]
fn launch_wsl(path: &str) -> Launch {
    let registry: pg_worker_core::hosts::HostRegistry = serde_json::from_value(json!({
        "version": 1,
        "self": "local",
        "hosts": [{
            "id": "worker", "aliases": [], "transport": "ssh", "ssh_target": "windows-hop",
            "wsl_distro": "Ubuntu", "env": {"PATH": path}, "os": "linux",
            "state_dir": "/srv/state", "repos": {}, "roles": ["implement", "review"],
            "caps": {"max_runs": 4, "max_writers": 2, "max_seats": 2,
                "max_sessions": 4, "mem_floor_mb": 6144},
            "warmup": {"warmup_s": 1, "ramp_mb": {"implement": 2048, "review": 1024}},
            "session_mb": 512
        }]
    }))
    .unwrap();
    let worker = registry.resolve("worker").unwrap();
    let candidate = pg_worker_core::package_placement::Candidate {
        id: worker.id.clone(),
        workstation: false,
        ssh: worker.ssh_route(),
        sample: None,
        readiness: None,
    };
    pg_worker_core::host_launch::launch(
        &registry,
        &candidate,
        &json!({"card":{},"prompt":"fixture"}),
    )
}

#[cfg(unix)]
#[test]
fn wsl_launch_sets_guest_path_before_spawnd() {
    with_ssh_fixture(
        stringify!(wsl_launch_sets_guest_path_before_spawnd),
        |dir| {
            let path = "/srv/worker/.local/bin:/usr/bin:/bin";
            assert!(matches!(launch_wsl(path), Launch::Started(_)));
            let argv = std::fs::read_to_string(dir.join("ssh-argv")).unwrap();
            assert_eq!(
                argv.lines().collect::<Vec<_>>(),
                [
                    "-o",
                    "BatchMode=yes",
                    "-o",
                    "ConnectTimeout=5",
                    "--",
                    "windows-hop",
                    "wsl.exe",
                    "--distribution",
                    "Ubuntu",
                    "--exec",
                    "/usr/bin/env",
                    "PATH=/srv/worker/.local/bin:/usr/bin:/bin",
                    "pg-spawnd",
                    "shim"
                ]
            );
        },
    );
}

#[cfg(unix)]
#[test]
fn wsl_launch_refuses_invalid_path_before_ssh() {
    with_ssh_fixture(
        stringify!(wsl_launch_refuses_invalid_path_before_ssh),
        |dir| {
            assert!(matches!(
                launch_wsl("/usr/bin:%PATH%"),
                Launch::Refused(reason) if reason == "invalid WSL PATH"
            ));
            assert!(!dir.join("ssh-argv").exists());
        },
    );
}

#[test]
fn a_confirmed_link_outage_queues_the_launch_with_a_named_reason() {
    use pg_worker_core::{host_launch::launch_with_state, host_link::observe_ssh};

    let state = tempfile::tempdir().unwrap();
    let registry = pg_worker_core::hosts::HostRegistry {
        version: 1,
        public_repos: Default::default(),
        self_host: "local".into(),
        hosts: vec![],
    };
    let host = pg_worker_core::package_placement::Candidate {
        id: "worker".into(),
        workstation: false,
        ssh: Some("worker.invalid".into()),
        sample: None,
        readiness: None,
    };
    for now in [100, 160] {
        observe_ssh(state.path(), "worker.invalid", now, false, || json!({})).unwrap();
    }
    assert!(matches!(
        launch_with_state(&registry, &host, &json!({}), state.path()),
        Launch::Waiting(reason) if reason == "worker unreachable since 100"
    ));
}
