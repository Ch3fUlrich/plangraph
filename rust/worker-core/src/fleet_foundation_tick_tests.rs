use super::*;
#[test]
fn fleet_foundation_clock() {
    let (a, _) = advance(None, 100_000, 100, 2);
    let (b, jump) = advance(Some(&a), 1000, 101, 2);
    assert_eq!(b.total_ms, 101_000);
    assert!(!jump);
    assert!(advance(Some(&b), 2000, 200, 2).1);
    assert!(advance(Some(&b), 2000, 50, 2).1);
}
#[test]
fn fleet_foundation_files() {
    let d = tempfile::tempdir().unwrap();
    let p = |n| d.path().join(n);
    let mut c = json!({"schema_version":1,"enabled":false,"stale_pair":true,
        "state_path":p("state"),"outbox_path":p("out"),"acks_path":p("acks"),
        "wall_jump_tolerance_s":2,"pairs":{"config":{"maximum_age":10,
        "divergence_tolerance":5,"compaction_cap":10,"silence_deadline":20,
        "retry_delay":5,"maximum_attempts":2},"groups":[
        {"group":"alpha","snapshot_path":p("alpha")},
        {"group":"beta","snapshot_path":p("beta")}]}});
    let tick = |c: &Value, t: u64| {
        fs::write(p("config"), c.to_string()).unwrap();
        invoke(
            &["tick".into()],
            Some(p("config")),
            t * 1000,
            100 + t as i64,
        )
    };
    assert_eq!(
        invoke(&["tick".into()], None, 0, 0).unwrap(),
        json!({"enabled":false})
    );
    assert_eq!(tick(&c, 0).unwrap(), json!({"enabled":false}));
    assert!(!p("state.lock").exists());
    c["enabled"] = json!(true);
    let mut bad = c.clone();
    bad.as_object_mut().unwrap().remove("pairs");
    assert!(tick(&bad, 0).is_err());
    bad["unknown"] = json!(true);
    assert!(tick(&bad, 0).is_err());
    assert!(!p("state.lock").exists());
    for (name, age) in [("alpha", 80), ("beta", 100)] {
        let members: Vec<_> = ["one", "two"]
            .iter()
            .map(|id| {
                json!({"identity":id,
            "revision":"r","checkpoint":"c","freshness":age,"provenance":"test"})
            })
            .collect();
        fs::write(
            p(name),
            json!({"members":members,"compactions":0}).to_string(),
        )
        .unwrap();
    }
    assert_eq!(
        tick(&c, 0).unwrap()["alarms_opened"],
        json!(["stale-pair:alpha:pair:1"])
    );
    assert_eq!(tick(&c, 1).unwrap()["attempts_sent"], 1);
    assert_eq!(tick(&c, 5).unwrap()["attempts_sent"], 2);
    assert_eq!(tick(&c, 6).unwrap()["attempts_sent"], 1);
    fs::write(p("acks"), "bad\nheartbeat:6\n").unwrap();
    assert_eq!(
        tick(&c, 7).unwrap()["diagnostics"],
        json!(["invalid acknowledgement"])
    );
    fs::remove_file(p("alpha")).unwrap();
    tick(&c, 26).unwrap();
    tick(&c, 27).unwrap();
    let text = fs::read_to_string(p("out")).unwrap();
    assert_eq!(
        text.matches("stale-pair:alpha:heartbeat:silence:1").count(),
        1
    );
    fs::write(p("acks"), "heartbeat:27\n").unwrap();
    tick(&c, 28).unwrap();
    tick(&c, 47).unwrap();
    assert!(fs::read_to_string(p("out")).unwrap().contains("silence:2"));
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

#[cfg(unix)]
#[test]
fn fleet_foundation_lock() {
    use std::os::fd::AsRawFd;
    let d = tempfile::tempdir().unwrap();
    let state = d.path().join("state");
    let lock = OpenOptions::new()
        .create(true)
        .truncate(false)
        .write(true)
        .open(d.path().join("state.lock"))
        .unwrap();
    assert_eq!(
        unsafe { libc::flock(lock.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) },
        0
    );
    let c = Config {
        schema_version: 1,
        enabled: true,
        stale_pair: false,
        state_path: Some(state.clone()),
        outbox_path: Some(d.path().join("out")),
        acks_path: Some(d.path().join("acks")),
        wall_jump_tolerance_s: Some(2),
        pairs: None,
    };
    assert_eq!(run(c, 0, 0).unwrap(), json!({"skipped":"locked"}));
    assert!(!state.exists());
}
