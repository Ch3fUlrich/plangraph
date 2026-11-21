use super::records;
use super::sandbox::{Launcher, Sandbox, LOCAL, SID};
use serde::Deserialize;
use serde_json::json;
use std::fs::{self, File};
use std::os::fd::AsRawFd;
use std::os::unix::fs::symlink;

#[derive(Deserialize)]
struct Rejection {
    name: String,
    role: String,
    args: Vec<String>,
    stderr: String,
}

pub fn check(launcher: &Launcher) {
    let cases: Vec<Rejection> =
        serde_json::from_str(include_str!("../fixtures/fleet-launch/refusals.json")).unwrap();
    let s = Sandbox::new(launcher);
    s.hosts(json!([
        {"name": LOCAL, "memory_floor_gib": 0},
        {"name": "fixture-disabled", "enabled": false, "memory_floor_gib": 0}
    ]));
    let out = s.command().output().unwrap();
    assert_eq!(out.status.code(), Some(2));
    assert!(s
        .normalize(&out.stdout)
        .contains("fleet-launch <role> <cwd> <model> <effort> <prompt-file>"));
    assert!(out.stderr.is_empty());
    for case in cases {
        let args = case
            .args
            .iter()
            .map(|arg| arg.replace("$ROOT", s.path("").to_str().unwrap().trim_end_matches('/')));
        let out = s
            .launch(&case.role)
            .args(args)
            .arg("--dry-run")
            .output()
            .unwrap();
        eprintln!("checking {}", case.name);
        s.expect(&out, 2, "", &case.stderr);
        s.no_launch();
    }

    let out = s
        .command()
        .arg("review")
        .arg(s.path("missing-work"))
        .args(["sonnet", "high"])
        .arg(s.path("prompt"))
        .arg("--dry-run")
        .output()
        .unwrap();
    assert_eq!(out.status.code(), Some(2));
    assert!(out.stdout.is_empty());
    assert_eq!(
        s.normalize(&out.stderr),
        "fleet-launch: cwd $ROOT/missing-work is not a directory\n"
    );
    let out = s
        .launch("review")
        .env("FLEET_LOCAL_HOST", "fixture-unknown")
        .args(["--respawn", "--dry-run"])
        .output()
        .unwrap();
    s.expect(&out, 2, "", "fleet-launch: respawn refused: this host is not in the placement policy and the saved spec names no host (set FLEET_LOCAL_HOST to its policy name)\n");
    s.no_launch();

    let s = Sandbox::new(launcher);
    fs::remove_file(s.path("prompt")).unwrap();
    let out = s.launch("review").output().unwrap();
    s.expect(
        &out,
        2,
        "",
        "fleet-launch: prompt file: [Errno 2] No such file or directory: '$ROOT/prompt'\n",
    );
    assert_eq!(records::ledger(&s, "ledger")[0]["result"], "refused");
    assert!(!s.path("child.json").exists());

    check_io_failures(launcher);
    check_child_failures(launcher);
    if matches!(launcher, Launcher::Pinned(_)) {
        check_enforcement(launcher);
    }
}

fn check_io_failures(launcher: &Launcher) {
    let s = Sandbox::new(launcher);
    fs::create_dir(s.path("state")).unwrap();
    File::create(s.path("state/launch-settings")).unwrap();
    let out = s.launch("review").output().unwrap();
    s.expect(&out, 2, "", "fleet-launch: launch settings for hard cap 800000: [Errno 17] File exists: '$ROOT/state/launch-settings'\n");
    assert_eq!(records::ledger(&s, "ledger")[0]["result"], "failed");
    assert!(!s.path("child.json").exists());

    let s = Sandbox::new(launcher);
    fs::create_dir_all(s.path("state/role-locks")).unwrap();
    let lock = File::create(s.path("state/role-locks/review.lock")).unwrap();
    // SAFETY: lock owns the live descriptor until the refusal has been observed.
    assert_eq!(
        unsafe { libc::flock(lock.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) },
        0
    );
    let out = s.launch("review").output().unwrap();
    s.expect(&out, 2, "", "fleet-launch: another launcher holds the role lock; no launch (a review session may be live or starting: check `claude agents --json`)\n");
    assert_eq!(records::ledger(&s, "ledger")[0]["result"], "refused");
    assert!(!s.path("state/session-seq").exists());
    assert!(!s.path("child.json").exists());

    let s = Sandbox::new(launcher);
    fs::create_dir_all(s.path("state/fleet-launch-fallback.jsonl")).unwrap();
    let out = s.launch("review").output().unwrap();
    s.expect(
        &out,
        2,
        "",
        "fleet-launch: fallback ledger unavailable; not starting\n",
    );
    assert!(!s.path("child.json").exists());
}

fn check_child_failures(launcher: &Launcher) {
    let s = Sandbox::new(launcher);
    fs::remove_file(s.path("bin/claude")).unwrap();
    symlink("/usr/bin/python3", s.path("bin/python3")).unwrap();
    let out = s
        .launch("review")
        .env("PATH", s.path("bin"))
        .output()
        .unwrap();
    s.expect(&out, 2, "", "fleet-launch: launch executable unavailable\n");
    assert_eq!(records::ledger(&s, "ledger")[0]["result"], "failed");

    let s = Sandbox::new(launcher);
    fs::write(s.path("child.rc"), "17").unwrap();
    let out = s.launch("review").output().unwrap();
    s.expect(&out, 17, &format!("{SID}\n"), "fixture child failure\n");
    assert_eq!(records::ledger(&s, "ledger")[0]["result"], "failed");
    assert_eq!(records::ledger(&s, "fallback")[0]["result"], "starting");
    assert!(!s.path("state/launch").exists());

    let s = Sandbox::new(launcher);
    fs::create_dir_all(s.path("state/fleet-launch-ledger.jsonl")).unwrap();
    let out = s.launch("review").output().unwrap();
    s.expect(
        &out,
        0,
        &format!("{SID}\nfleet-launch: session review-0001\nfleet-launch: started {SID}\n"),
        "fleet-launch: admission ledger unavailable\n",
    );
    assert_eq!(
        records::ledger(&s, "fallback")
            .iter()
            .map(|row| row["result"].as_str().unwrap())
            .collect::<Vec<_>>(),
        ["starting", "started"]
    );
    assert!(s.path("state/launch/review.json").is_file());
}

fn check_enforcement(launcher: &Launcher) {
    let s = Sandbox::new(launcher);
    let path = "router/ops/fleet-launch-policy.json";
    let mut policy = s.json(path);
    policy["enforce"] = json!(true);
    s.write_json(path, policy);
    let mut cmd = s.command();
    cmd.arg("review")
        .arg(s.path("work"))
        .args(["sonnet", "high"])
        .arg(s.path("prompt"));
    let out = cmd.arg("--dry-run").output().unwrap();
    let missing = "missing: owner, card or track, reason, cap, handoff";
    s.expect(
        &out,
        2,
        "",
        &format!("fleet-launch: admission refused: {missing}\n"),
    );
    s.no_launch();

    let out = s
        .command()
        .arg("review")
        .arg(s.path("work"))
        .args(["sonnet", "high"])
        .arg(s.path("prompt"))
        .output()
        .unwrap();
    s.expect(
        &out,
        2,
        "",
        &format!("fleet-launch: admission: {missing}\n"),
    );
    let row = &records::ledger(&s, "ledger")[0];
    assert_eq!(row["result"], "refused");
    assert_eq!(row["enforce"], true);
    assert_eq!(
        row["missing"],
        json!(["owner", "card or track", "reason", "cap", "handoff"])
    );
    assert!(!s.path("child.json").exists());
}
