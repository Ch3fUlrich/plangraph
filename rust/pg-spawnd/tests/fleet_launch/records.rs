use super::sandbox::{Launcher, Sandbox, PROMPT, SID};
use serde_json::{json, Value};
use std::fs::{self, File};
use std::os::fd::AsRawFd;
use std::os::unix::fs::PermissionsExt;
use std::time::{SystemTime, UNIX_EPOCH};

pub fn ledger(s: &Sandbox, name: &str) -> Vec<Value> {
    let path = s.path(&format!("state/fleet-launch-{name}.jsonl"));
    assert_eq!(
        fs::metadata(&path).unwrap().permissions().mode() & 0o777,
        0o600
    );
    fs::read_to_string(path)
        .unwrap()
        .lines()
        .map(|line| serde_json::from_str(line).unwrap())
        .collect()
}

fn without_timestamp(mut record: Value) -> Value {
    let timestamp = record.as_object_mut().unwrap().remove("ts").unwrap();
    assert!(timestamp.as_u64().is_some_and(|ts| ts > 0));
    record
}

pub fn check(launcher: &Launcher) {
    let s = Sandbox::new(launcher);
    for dir in [
        "home/.nvm/versions/node/v22/bin",
        "home/.nvm/versions/node/v20/bin",
        "home/.qoder/entry",
    ] {
        fs::create_dir_all(s.path(dir)).unwrap();
    }
    let out = s
        .launch("review")
        .args([
            "--parent",
            "parent-0042",
            "--ctx-guard",
            "110000,170000",
            "--oom-protect",
        ])
        .env("FLEET_FORWARDED", "1")
        .output()
        .unwrap();
    s.expect(
        &out,
        0,
        &format!("{SID}\nfleet-launch: session review-0001\nfleet-launch: started {SID}\n"),
        "",
    );
    let role = s.json("state/launch/review.json");
    assert_eq!(role, s.json("state/launch/sessions/review-0001.json"));
    let expected = include_str!("../fixtures/fleet-launch/launch-record.json")
        .replace("$ROOT", s.path("").to_str().unwrap().trim_end_matches('/'));
    let expected: Value = serde_json::from_str(&expected).unwrap();
    assert_eq!(without_timestamp(role.clone()), expected);
    let expected_ledger: Value =
        serde_json::from_str(include_str!("../fixtures/fleet-launch/ledger.json")).unwrap();
    let rows = ledger(&s, "ledger");
    assert_eq!(rows.len(), 1);
    assert_eq!(without_timestamp(rows[0].clone()), expected_ledger);
    let rows = ledger(&s, "fallback");
    assert_eq!(rows.len(), 1);
    let mut starting = expected_ledger;
    starting["result"] = json!("starting");
    assert_eq!(without_timestamp(rows[0].clone()), starting);

    let child = s.json("child.json");
    assert_eq!(
        child["argv"].as_array().unwrap().last(),
        Some(&json!(PROMPT))
    );
    assert_eq!(child["cwd"], json!(s.path("work")));
    assert_eq!(child["role"], "review");
    assert_eq!(child["window"], "400000");
    assert_eq!(child["forwarded"], Value::Null);
    let expected_path = format!(
        "{}:{}:{}:{}:/usr/bin:/bin",
        s.path("home/.nvm/versions/node/v22/bin").display(),
        s.path("home/.nvm/versions/node/v20/bin").display(),
        s.path("home/.qoder/entry").display(),
        s.path("bin").display()
    );
    assert_eq!(child["path"], expected_path);
    for path in [
        "state/launch/review.json",
        "state/launch/sessions/review-0001.json",
        "state/launch-settings/caps-110000-170000.json",
        "state/launch/children/parent.txt",
        "state/role-locks/review.lock",
    ] {
        assert_eq!(
            fs::metadata(s.path(path)).unwrap().permissions().mode() & 0o777,
            0o600,
            "{path}"
        );
    }
    let settings = s.json("state/launch-settings/caps-110000-170000.json");
    for (key, value) in [
        ("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "400000"),
        ("CLAUDE_CODE_IDLE_COMPACT_MIN_TOKENS", "400000"),
        ("AUTOOS_CTX_HANDOFF", "110000"),
        ("AUTOOS_CTX_HARD", "170000"),
    ] {
        assert_eq!(settings["env"][key], value);
    }
    assert_eq!(fs::read_dir(s.path("work")).unwrap().count(), 0);
    assert_eq!(
        fs::read_to_string(s.path("state/launch/children/parent.txt")).unwrap(),
        "review-0001\n"
    );
    assert!(!s.path("state/launch/parent.json").exists());
    assert_eq!(
        s.json(&format!("guard/{SID}.json")),
        json!({"handoff": 110000, "hard": 170000, "attended_window_s": 1800})
    );
    assert_eq!(
        fs::read_to_string(s.path("oom/oom-protect-sessions")).unwrap(),
        format!("{SID} review-0001\n")
    );

    check_respawn(&s, role);
}

fn check_respawn(s: &Sandbox, original: Value) {
    let watchdog_lock = File::open(s.path("state/role-locks/review.lock")).unwrap();
    // SAFETY: the fixture holds the same live descriptor until the child exits.
    assert_eq!(
        unsafe { libc::flock(watchdog_lock.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) },
        0
    );
    let replacement_prompt = s.path("replacement");
    fs::write(&replacement_prompt, "Replacement prompt").unwrap();
    let out = s
        .command()
        .args(["review", s.path("work").to_str().unwrap(), "haiku", "low"])
        .arg(&replacement_prompt)
        .args(["--respawn", "--session", "review-0001"])
        .env("FLEET_DEFER_RECORD", "1")
        .output()
        .unwrap();
    s.expect(
        &out,
        0,
        &format!("{SID}\nfleet-launch: session review-0002\nfleet-launch: started {SID}\n"),
        "",
    );
    assert_eq!(s.json("state/launch/review.json"), original);
    assert_eq!(
        fs::metadata(s.path("state/launch/review.pending.json"))
            .unwrap()
            .permissions()
            .mode()
            & 0o777,
        0o600
    );
    let pending = s.json("state/launch/review.pending.json");
    assert_eq!(pending, s.json("state/launch/sessions/review-0002.json"));
    let mut expected = without_timestamp(original);
    expected["model"] = json!("haiku");
    expected["effort"] = json!("low");
    expected["seq"] = json!(2);
    expected["session_name"] = json!("review-0002");
    assert_eq!(without_timestamp(pending), expected);
    assert_eq!(
        fs::read_to_string(s.path("state/session-seq"))
            .unwrap()
            .trim(),
        "2"
    );
    assert_eq!(s.json("child.json")["defer"], Value::Null);
    assert_eq!(
        fs::read_to_string(s.path("state/launch/children/parent.txt")).unwrap(),
        "review-0001\n"
    );
    assert_eq!(
        fs::read_to_string(s.path("oom/oom-protect-sessions")).unwrap(),
        format!("{SID} review-0001\n")
    );
}

pub fn check_id_resolution(launcher: &Launcher) {
    let s = Sandbox::new(launcher);
    fs::write(s.path("child.stdout"), "backgrounded 12345678\n").unwrap();
    s.write_json(
        "agents.json",
        json!([
            {"id":"12345678", "sessionId": "12345678-0000-0000-0000-000000000000",
                "kind":"background", "name":"review-0123", "cwd":"/other"},
            {"id":"12345678", "sessionId":SID, "kind":"background",
                "name":"review-0123", "cwd":s.path("work")},
        ]),
    );
    let out = s
        .launch("review")
        .env("FLEET_SESSION_SEQ", "123")
        .output()
        .unwrap();
    s.expect(&out, 0, &format!("backgrounded 12345678\nfleet-launch: session review-0123\nfleet-launch: started {SID}\n"), "");
    assert_eq!(s.json("state/launch/review.json")["session_id"], SID);
    assert!(!s.path("state/session-seq").exists());

    let s = Sandbox::new(launcher);
    fs::write(s.path("child.stdout"), "backgrounded no-id\n").unwrap();
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_millis() as u64;
    s.write_json(
        "agents.json",
        json!([
            {"name":"review-0001", "kind":"background", "cwd":s.path("work"),
                "sessionId": "11111111-0000-0000-0000-000000000000", "startedAt":1},
            {"name":"review-0001", "kind":"background", "cwd":s.path("work"),
                "sessionId": SID, "startedAt":now},
            {"name":"review-0002", "kind":"background", "cwd":s.path("work"),
                "sessionId": "22222222-0000-0000-0000-000000000000", "startedAt":now + 1}
        ]),
    );
    let out = s.launch("review").output().unwrap();
    s.expect(
        &out,
        0,
        &format!(
            "backgrounded no-id\nfleet-launch: session review-0001\nfleet-launch: started {SID}\n"
        ),
        "",
    );
    assert_eq!(s.json("state/launch/review.json")["session_id"], SID);

    let s = Sandbox::new(launcher);
    fs::write(s.path("child.stdout"), "backgrounded no-id\n").unwrap();
    fs::write(s.path("agents.json"), "unfinished JSON").unwrap();
    let out = s
        .launch("review")
        .args(["--oom-protect", "--ctx-guard", "1,2"])
        .output()
        .unwrap();
    s.expect(&out, 0, "backgrounded no-id\nfleet-launch: session review-0001\nfleet-launch: started-unknown review-0001\n",
        concat!("fleet-launch: warning: no session id for review-0001; fleet hooks will not find this session\n",
            "fleet-launch: warning: no session id for review-0001; oom_protect/ctx_guard not applied\n"));
    assert_eq!(
        s.json("state/launch/review.json")["session_id"],
        Value::Null
    );
    assert!(!s.path("oom").exists() && !s.path("guard").exists());
}
