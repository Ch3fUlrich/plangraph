use super::*;
#[test]
#[serial_test::serial(fleet_relative_state_parent)]
fn fleet_foundation_clock() {
    let (a, _) = advance(None, 100_000, 100, 2);
    let (b, jump) = advance(Some(&a), 1000, 101, 2);
    assert_eq!(b.total_ms, 101_000);
    assert!(!jump);
    assert!(advance(Some(&b), 2000, 200, 2).1);
    assert!(advance(Some(&b), 2000, 50, 2).1);
}
pub(super) fn foundation_setup() -> (tempfile::TempDir, Value) {
    let d = tempfile::tempdir().unwrap();
    let p = |n| d.path().join(n);
    let mut c = json!({"schema_version":1,"enabled":false,"stale_pair":true,
        "state_path":p("state"),"outbox_path":p("out"),"acks_path":p("acks"),
        "wall_jump_tolerance_s":2,"pairs":{"config":{"maximum_age":10,
        "divergence_tolerance":5,"compaction_cap":10,"silence_deadline":20,
        "retry_delay":5,"maximum_attempts":3},"groups":[
        {"group":"alpha","snapshot_path":p("alpha")},
        {"group":"beta","snapshot_path":p("beta")}]}});
    c["enabled"] = json!(true);
    (d, c)
}
pub(super) fn foundation_tick(d: &tempfile::TempDir, c: &Value, t: u64) -> Result<Value, String> {
    fs::write(d.path().join("config"), c.to_string()).unwrap();
    invoke(
        &["tick".into()],
        Some(d.path().join("config")),
        t * 1000,
        100 + t as i64,
    )
}
#[test]
#[serial_test::serial(fleet_relative_state_parent)]
fn fleet_foundation_default_off() {
    let (d, mut c) = foundation_setup();
    let p = |n| d.path().join(n);
    let tick = |c: &Value, t| foundation_tick(&d, c, t);
    c["enabled"] = json!(false);
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
}
pub(super) fn foundation_samples(d: &tempfile::TempDir) {
    let p = |n| d.path().join(n);
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
}
#[test]
#[serial_test::serial(fleet_relative_state_parent)]
fn fleet_foundation_group_isolation() {
    let (d, c) = foundation_setup();
    foundation_samples(&d);
    let p = |n| d.path().join(n);
    let tick = |c: &Value, t| foundation_tick(&d, c, t);
    assert_eq!(
        tick(&c, 0).unwrap()["alarms_opened"],
        json!(["stale-pair:alpha:pair:1"])
    );
    let state = || serde_json::from_slice::<Value>(&fs::read(p("state")).unwrap()).unwrap();
    assert!(state()["pairs"]["beta"].is_object());
}
#[test]
#[serial_test::serial(fleet_relative_state_parent)]
fn fleet_foundation_retry_ack() {
    let (d, mut c) = foundation_setup();
    foundation_samples(&d);
    let p = |n| d.path().join(n);
    let tick = |c: &Value, t| foundation_tick(&d, c, t);
    tick(&c, 0).unwrap();
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
}
#[test]
#[serial_test::serial(fleet_relative_state_parent)]
fn fleet_foundation_heartbeat() {
    let (d, mut c) = foundation_setup();
    foundation_samples(&d);
    let p = |n| d.path().join(n);
    let tick = |c: &Value, t| foundation_tick(&d, c, t);
    tick(&c, 0).unwrap();
    let state = || serde_json::from_slice::<Value>(&fs::read(p("state")).unwrap()).unwrap();
    let beta = serde_json::to_vec(&state()["pairs"]["beta"]).unwrap();
    c["pairs"]["groups"].as_array_mut().unwrap().pop();
    fs::write(p("acks"), "heartbeat:6\nstale-pair:alpha:pair:1\n").unwrap();
    tick(&c, 10).unwrap();
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
    for line in fs::read_to_string(p("out"))
        .unwrap()
        .lines()
        .map(|line| serde_json::from_str::<Value>(line).unwrap())
        .filter(|line| line["kind"] == "alarm")
    {
        assert_eq!(line["group"], "alpha");
    }
    fs::write(p("alpha"), alpha).unwrap();
}

#[test]
#[serial_test::serial(fleet_relative_state_parent)]
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
#[serial_test::serial(fleet_relative_state_parent)]
fn fleet_hysteresis_disabled_and_error_save() {
    let (d, mut c) = foundation_setup();
    foundation_samples(&d);
    let rules = crate::fleet_router_hysteresis::NAMES
        .iter()
        .zip([
            "tokens",
            "events",
            "asks",
            "IDs/second",
            "claims",
            "seconds",
        ])
        .map(|(name, unit)| {
            json!({"name":name,"unit":unit,"cadence":10,
            "high":2,"low":1,"enter":3,"exit":2,"freshness":10})
        })
        .collect::<Vec<_>>();
    c["hysteresis_config"] = json!({"tick_seconds":10,"rules":{"schema_version":1,
        "signals":rules},"sources":[d.path().join("unread"),d.path().join("unread"),
        d.path().join("unread")],"retry_delay":1,"maximum_attempts":3,"silence_after":1000});
    let source = d.path().join("unread");
    fs::write(&source, "invalid source").unwrap();
    #[cfg(target_os = "linux")]
    let watch = {
        use std::os::fd::FromRawFd;
        let fd = unsafe { libc::inotify_init1(libc::IN_NONBLOCK | libc::IN_CLOEXEC) };
        assert!(fd >= 0);
        let path = std::ffi::CString::new(source.to_str().unwrap()).unwrap();
        assert!(unsafe { libc::inotify_add_watch(fd, path.as_ptr(), libc::IN_ACCESS) } >= 0);
        unsafe { fs::File::from_raw_fd(fd) }
    };
    foundation_tick(&d, &c, 0).unwrap();
    #[cfg(target_os = "linux")]
    {
        use std::io::Read;
        let mut bytes = [0; 256];
        assert_eq!(
            (&watch).read(&mut bytes).unwrap_err().kind(),
            std::io::ErrorKind::WouldBlock
        );
    }
    let path = d.path().join("state");
    let mut state: Value = serde_json::from_slice(&fs::read(&path).unwrap()).unwrap();
    assert!(state.get("hysteresis").is_none());
    c["hysteresis"] = json!(true);
    foundation_tick(&d, &c, 1).unwrap();
    state = serde_json::from_slice(&fs::read(&path).unwrap()).unwrap();
    state["hysteresis"]["evaluator"]["states"]["unexpected"] = json!({
        "last":null,"entering":0,"exiting":0,"episode":0,"latched":false});
    fs::write(&path, state.to_string()).unwrap();
    let result = foundation_tick(&d, &c, 11).unwrap();
    assert!(result["diagnostics"]
        .as_array()
        .unwrap()
        .contains(&json!("unconfigured state")));
    let state: Value = serde_json::from_slice(&fs::read(path).unwrap()).unwrap();
    assert_eq!(state["clock"]["total_ms"], 11000);
    assert!(fs::read_to_string(d.path().join("out"))
        .unwrap()
        .contains("heartbeat:11"));
}
