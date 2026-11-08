use super::tests::{foundation_samples, foundation_setup, foundation_tick};
use super::*;
#[test]
#[serial_test::serial(fleet_relative_state_parent)]
fn fleet_relative_state_parent() {
    const CHILD: &str = "PG_FLEET_RELATIVE_STATE_CHILD";
    if std::env::var_os(CHILD).is_none() {
        let d = tempfile::tempdir().unwrap();
        let status = std::process::Command::new(std::env::current_exe().unwrap())
            .args([
                "--exact",
                std::thread::current().name().unwrap(),
                "--nocapture",
            ])
            .env(CHILD, "1")
            .current_dir(d.path())
            .status()
            .unwrap();
        assert!(status.success());
        assert_eq!(fs::read_dir(d.path()).unwrap().count(), 0);
        d.close().unwrap();
        return;
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        struct Cleanup;
        impl Drop for Cleanup {
            fn drop(&mut self) {
                fs::set_permissions(".", fs::Permissions::from_mode(0o700)).unwrap();
                for path in ["state.json", "state.json.lock", "out"] {
                    if Path::new(path).exists() {
                        fs::remove_file(path).unwrap();
                    }
                }
            }
        }
        let _cleanup = Cleanup;
        let writable = tempfile::tempdir().unwrap();
        let outbox = writable.path().join("out");
        let config = json!({"schema_version":1,"enabled":true,"state_path":"state.json",
            "outbox_path":outbox,"acks_path":"acks","wall_jump_tolerance_s":2});
        fs::write(
            "state.json",
            br#"{"schema_version":1,"clock":null,"pairs":{}}"#,
        )
        .unwrap();
        run(serde_json::from_value(config.clone()).unwrap(), 0, 0).unwrap();
        assert!(serde_json::from_slice::<Value>(&fs::read("state.json").unwrap()).is_ok());
        assert!(outbox.exists());
        fs::remove_file(&outbox).unwrap();
        let saved = fs::read("state.json").unwrap();
        // Keep the existing lock writable, but deny creation of the save tempfile.
        fs::set_permissions(".", fs::Permissions::from_mode(0o500)).unwrap();
        let result = run(serde_json::from_value(config).unwrap(), 1000, 1);
        fs::set_permissions(".", fs::Permissions::from_mode(0o700)).unwrap();
        assert_eq!(result.unwrap_err(), "state temporary file");
        assert_eq!(fs::read("state.json").unwrap(), saved);
        assert!(!outbox.exists());
    }
}
#[test]
#[serial_test::serial(fleet_relative_state_parent)]
fn fleet_foundation_outbox_failure() {
    let (d, mut c) = foundation_setup();
    foundation_samples(&d);
    let p = |n| d.path().join(n);
    let tick = |c: &Value, t| foundation_tick(&d, c, t);
    let state = || serde_json::from_slice::<Value>(&fs::read(p("state")).unwrap()).unwrap();
    let lines = || {
        fs::read_to_string(p("out"))
            .unwrap()
            .lines()
            .map(|line| serde_json::from_str::<Value>(line).unwrap())
            .collect::<Vec<_>>()
    };
    c["pairs"]["groups"].as_array_mut().unwrap().pop();
    c["outbox_path"] = json!(d.path());
    let failed = tick(&c, 0).unwrap();
    assert_eq!(failed["attempts_sent"], 2);
    assert_eq!(
        failed["diagnostics"],
        json!(["outbox append failed", "outbox append failed"])
    );
    let saved = state();
    assert_eq!(saved["clock"]["total_ms"], 0);
    assert_eq!(
        saved["pairs"]["alpha"]["delivery"]["primary"]["records"]["stale-pair:alpha:pair:1"]
            ["attempts"],
        1
    );
    c["outbox_path"] = json!(p("out"));
    assert_eq!(tick(&c, 5).unwrap()["attempts_sent"], 2);
    assert_eq!(lines()[0]["id"], "stale-pair:alpha:pair:1");
    assert_eq!(tick(&c, 10).unwrap()["attempts_sent"], 2);
    assert_eq!(tick(&c, 15).unwrap()["attempts_sent"], 1);
    assert_eq!(
        lines()
            .iter()
            .filter(|l| l["id"] == "stale-pair:alpha:pair:1")
            .count(),
        2
    );
    tick(&c, 20).unwrap();
    tick(&c, 21).unwrap();
    assert_eq!(
        fs::read_to_string(p("out"))
            .unwrap()
            .matches("heartbeat:silence:1")
            .count(),
        1
    );
    fs::write(p("acks"), "stale-pair:alpha:pair:1\nheartbeat:21\n").unwrap();
    let mut fresh: Value = serde_json::from_slice(&fs::read(p("alpha")).unwrap()).unwrap();
    for member in fresh["members"].as_array_mut().unwrap() {
        member["freshness"] = json!(122);
    }
    fs::write(p("alpha"), fresh.to_string()).unwrap();
    assert_eq!(tick(&c, 22).unwrap()["attempts_sent"], 1);
    assert_eq!(
        tick(&c, 32).unwrap()["alarms_opened"],
        json!(["stale-pair:alpha:pair:2"])
    );
    assert_eq!(
        lines()
            .iter()
            .filter(|l| l["id"] == "stale-pair:alpha:pair:2")
            .count(),
        1
    );
}
#[test]
#[serial_test::serial(fleet_relative_state_parent)]
fn fleet_foundation_corrupt_state() {
    let (d, c) = foundation_setup();
    foundation_samples(&d);
    let p = |n| d.path().join(n);
    let tick = |c: &Value, t| foundation_tick(&d, c, t);
    tick(&c, 0).unwrap();
    let original = fs::read(p("out")).unwrap();
    for bytes in [
        b"bad".as_slice(),
        br#"{"schema_version":2,"clock":null,"pairs":{}}"#,
    ] {
        fs::write(p("state"), bytes).unwrap();
        assert!(tick(&c, 28).is_err());
        assert_eq!(fs::read(p("state")).unwrap(), bytes);
        assert_eq!(fs::read(p("out")).unwrap(), original);
    }
}

#[test]
#[serial_test::serial(fleet_relative_state_parent)]
fn fleet_foundation_lock() {
    let d = tempfile::tempdir().unwrap();
    let state = d.path().join("state");
    let _lock = crate::lockfile::try_lock(&d.path().join("state.lock"))
        .unwrap()
        .expect("exclusive lock acquired");
    let c = Config {
        schema_version: 1,
        enabled: true,
        stale_pair: false,
        hysteresis: false,
        hysteresis_config: None,
        seat_supply: false,
        seat_supply_config: None,
        state_path: Some(state.clone()),
        outbox_path: Some(d.path().join("out")),
        acks_path: Some(d.path().join("acks")),
        wall_jump_tolerance_s: Some(2),
        pairs: None,
    };
    assert!(crate::lockfile::try_lock(&d.path().join("state.lock"))
        .unwrap()
        .is_none());
    assert_eq!(run(c, 0, 0).unwrap(), json!({"skipped":"locked"}));
    assert!(!state.exists());
}
