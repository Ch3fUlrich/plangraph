use super::*;
#[test]
fn fleet_relative_state_parent() {
    let d = tempfile::tempdir().unwrap();
    let config = json!({"schema_version":1,"enabled":true,"state_path":"state.json",
        "outbox_path":d.path().join("out"),"acks_path":d.path().join("acks"),
        "wall_jump_tolerance_s":2});
    let file = tempfile::NamedTempFile::new_in(".").unwrap();
    let bare = PathBuf::from(file.path().file_name().unwrap());
    let mut c: Config = serde_json::from_value(config.clone()).unwrap();
    c.state_path = Some(bare.clone());
    fs::write(&bare, br#"{"schema_version":1,"clock":null,"pairs":{}}"#).unwrap();
    run(c, 0, 0).unwrap();
    assert!(serde_json::from_slice::<Value>(&fs::read(&bare).unwrap()).is_ok());
    assert!(d.path().join("out").exists());
    fs::remove_file(format!("{}.lock", bare.display())).unwrap();
    fs::remove_file(d.path().join("out")).unwrap();
    let mut c: Config = serde_json::from_value(config).unwrap();
    c.state_path = Some(d.path().join("missing/state.json"));
    assert!(run(c, 0, 0).is_err());
    assert!(!d.path().join("out").exists());
}
#[test]
fn fleet_foundation_clock() {
    let (a, _) = advance(None, 100_000, 100, 2);
    let (b, jump) = advance(Some(&a), 1000, 101, 2);
    assert_eq!(b.total_ms, 101_000);
    assert!(!jump);
    assert!(advance(Some(&b), 2000, 200, 2).1);
    assert!(advance(Some(&b), 2000, 50, 2).1);
}
fn foundation_clause(clause: u8) {
    let d = tempfile::tempdir().unwrap();
    let p = |n| d.path().join(n);
    let mut c = json!({"schema_version":1,"enabled":false,"stale_pair":true,
        "state_path":p("state"),"outbox_path":p("out"),"acks_path":p("acks"),
        "wall_jump_tolerance_s":2,"pairs":{"config":{"maximum_age":10,
        "divergence_tolerance":5,"compaction_cap":10,"silence_deadline":20,
        "retry_delay":5,"maximum_attempts":3},"groups":[
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
    if clause == 0 {
        return;
    }
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
    let state = || serde_json::from_slice::<Value>(&fs::read(p("state")).unwrap()).unwrap();
    let beta = serde_json::to_vec(&state()["pairs"]["beta"]).unwrap();
    if clause == 1 {
        return;
    }
    c["pairs"]["groups"].as_array_mut().unwrap().pop();
    assert_eq!(tick(&c, 1).unwrap()["attempts_sent"], 1);
    assert_eq!(tick(&c, 5).unwrap()["attempts_sent"], 2);
    assert_eq!(tick(&c, 6).unwrap()["attempts_sent"], 1);
    let lines = || {
        fs::read_to_string(p("out"))
            .unwrap()
            .lines()
            .map(|line| serde_json::from_str::<Value>(line).unwrap())
            .collect::<Vec<_>>()
    };
    assert_eq!(lines()[3]["id"], "stale-pair:alpha:pair:1");
    fs::write(p("acks"), "bad\nheartbeat:6\nstale-pair:alpha:pair:1\n").unwrap();
    assert_eq!(
        tick(&c, 7).unwrap()["diagnostics"],
        json!(["invalid acknowledgement"])
    );
    assert_eq!(tick(&c, 10).unwrap()["attempts_sent"], 1);
    if clause == 2 {
        return;
    }
    let alpha = fs::read(p("alpha")).unwrap();
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
    assert_eq!(serde_json::to_vec(&state()["pairs"]["beta"]).unwrap(), beta);
    for line in lines().iter().filter(|line| line["kind"] == "alarm") {
        assert_eq!(line["group"], "alpha");
    }
    if clause == 3 {
        return;
    }
    fs::remove_file(p("acks")).unwrap();
    fs::remove_file(p("state")).unwrap();
    fs::remove_file(p("out")).unwrap();
    fs::write(p("alpha"), alpha).unwrap();
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
    if clause == 4 {
        return;
    }
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
        hysteresis: false,
        hysteresis_config: None,
        state_path: Some(state.clone()),
        outbox_path: Some(d.path().join("out")),
        acks_path: Some(d.path().join("acks")),
        wall_jump_tolerance_s: Some(2),
        pairs: None,
    };
    assert_eq!(run(c, 0, 0).unwrap(), json!({"skipped":"locked"}));
    assert!(!state.exists());
}

#[test]
fn fleet_foundation_missing_episode() {
    let finding = pair::Finding {
        evidence: None,
        ages: [None; 2],
        divergence: None,
        elapsed_compactions: None,
        diagnostics: vec![],
        reasons: vec![],
        episode: None,
        new_alarm: true,
    };
    let (mut active, mut opened, mut diagnostics) = (Some("previous".into()), vec![], vec![]);
    record_alarm(
        &finding,
        "stale-pair:alpha",
        &mut active,
        &mut opened,
        &mut diagnostics,
    );
    assert_eq!(active.as_deref(), Some("previous"));
    assert!(opened.is_empty());
    assert_eq!(diagnostics, ["new alarm missing episode"]);
}

#[test]
fn fleet_foundation_default_off() {
    foundation_clause(0);
}

#[test]
fn fleet_foundation_group_isolation() {
    foundation_clause(1);
}

#[test]
fn fleet_foundation_retry_ack() {
    foundation_clause(2);
}

#[test]
fn fleet_foundation_heartbeat() {
    foundation_clause(3);
}

#[test]
fn fleet_foundation_outbox_failure() {
    foundation_clause(4);
}

#[test]
fn fleet_foundation_corrupt_state() {
    foundation_clause(5);
}
