use super::{
    records::ledger,
    sandbox::{Launcher, Sandbox},
    test_python,
};
use std::{
    ffi::CString,
    fs::{self, File, OpenOptions},
    os::fd::AsRawFd,
    os::unix::process::ExitStatusExt,
    os::unix::{
        ffi::OsStrExt,
        fs::{OpenOptionsExt, PermissionsExt},
    },
    process::Stdio,
    thread,
    time::{Duration, Instant},
};

pub fn check(launcher: &Launcher) {
    super::remote::check_interruption(launcher);
    check_private_ledgers(launcher);
    for phase in ["background", "agents"] {
        interrupted(launcher, matches!(launcher, Launcher::Rust(_)), phase);
    }
}

// Counter protocol from the pinned fleet-launch next_seq function. The
// sentinel lets the fixture release its contention barrier in either order.
const PYTHON_COUNTER: &str = r#"
import fcntl, os, pathlib
path = pathlib.Path(os.environ["ROUTING_STATE_DIR"]) / "session-seq"
with path.open("a+") as f:
    pathlib.Path("python-ready").touch()
    fcntl.flock(f, fcntl.LOCK_EX)
    f.seek(0)
    n = int(f.read().strip() or 0) + 1
    f.seek(0)
    f.truncate()
    f.write(str(n))
print(n)
"#;

pub fn check_counter(launcher: &Launcher, fixture: bool) {
    let s = Sandbox::new(launcher);
    fs::create_dir(s.path("state")).unwrap();
    fs::write(s.path("state/session-seq"), "9998").unwrap();
    let seed = s.direct_launch("seed", true).output().unwrap();
    assert!(seed.status.success(), "{}", s.normalize(&seed.stderr));
    let (mut peer, ready) = if fixture {
        let mut command = test_python::command();
        s.isolate(&mut command);
        command.args(["-c", PYTHON_COUNTER]);
        (command, s.path("python-ready"))
    } else {
        (
            s.direct_launch("second-seat", matches!(launcher, Launcher::Rust(_))),
            s.path("state/role-locks/second-seat.lock"),
        )
    };

    let counter = File::open(s.path("state/session-seq")).unwrap();
    // SAFETY: the fixture owns the live descriptor until the barrier is released.
    assert_eq!(
        unsafe { libc::flock(counter.as_raw_fd(), libc::LOCK_EX) },
        0
    );
    let rust = s
        .direct_launch("rust-seat", true)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    let peer = peer
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    let mut waiting = false;
    let deadline = Instant::now() + Duration::from_secs(5);
    while Instant::now() < deadline {
        if ready.exists() && s.path("state/role-locks/rust-seat.lock").exists() {
            waiting = true;
            break;
        }
        thread::sleep(Duration::from_millis(5));
    }
    drop(counter);
    let peer = peer.wait_with_output().unwrap();
    let rust = rust.wait_with_output().unwrap();
    assert!(
        waiting,
        "both launchers must reach allocation while the counter is locked"
    );
    for output in [&peer, &rust] {
        assert!(output.status.success(), "{}", s.normalize(&output.stderr));
    }
    let mut names: Vec<_> = fs::read_dir(s.path("state/launch/sessions"))
        .unwrap()
        .map(|entry| {
            let path = entry.unwrap().path();
            let doc: serde_json::Value = serde_json::from_slice(&fs::read(path).unwrap()).unwrap();
            doc["seq"].as_u64().unwrap()
        })
        .collect();
    if fixture {
        names.push(
            String::from_utf8(peer.stdout)
                .unwrap()
                .trim()
                .parse()
                .unwrap(),
        );
    }
    names.sort();
    assert_eq!(names, [9999, 10000, 10001]);
    assert_eq!(
        fs::read_to_string(s.path("state/session-seq")).unwrap(),
        "10001"
    );
    assert_eq!(ledger(&s, "ledger").len(), if fixture { 2 } else { 3 });
}

fn check_private_ledgers(launcher: &Launcher) {
    let s = Sandbox::new(launcher);
    let policy_path = "router/ops/fleet-launch-policy.json";
    let mut policy = s.json(policy_path);
    policy["ledger"] = serde_json::json!("logs/private/admission.jsonl");
    s.write_json(policy_path, policy.clone());
    let real = s.path("real");
    fs::create_dir(&real).unwrap();
    std::os::unix::fs::symlink(&real, s.path("alias")).unwrap();
    let output = s
        .launch("review")
        .env("ROUTING_STATE_DIR", s.path("alias/state"))
        .output()
        .unwrap();
    assert!(output.status.success(), "{}", s.normalize(&output.stderr));
    let logs = real.join("state/logs");
    for dir in [&logs, &logs.join("private")] {
        assert_eq!(
            fs::metadata(dir).unwrap().permissions().mode() & 0o777,
            0o700
        );
    }
    let file = logs.join("private/admission.jsonl");
    assert_eq!(
        fs::metadata(&file).unwrap().permissions().mode() & 0o777,
        0o600
    );
    let row: serde_json::Value = serde_json::from_slice(&fs::read(file).unwrap()).unwrap();
    assert_eq!(row["result"], "started");

    policy["ledger"] = serde_json::json!("../escaped.jsonl");
    s.write_json(policy_path, policy);
    let output = s.launch("review").output().unwrap();
    assert!(output.status.success());
    assert_eq!(
        s.normalize(&output.stderr),
        "fleet-launch: admission: missing or invalid policy\n"
    );
    assert!(!s.path("escaped.jsonl").exists());
    assert_eq!(
        ledger(&s, "ledger")[0]["policy_error"],
        "missing or invalid policy"
    );

    let s = Sandbox::new(launcher);
    let fallback = s.path("state/fleet-launch-fallback.jsonl");
    fs::create_dir(s.path("state")).unwrap();
    let name = CString::new(fallback.as_os_str().as_bytes()).unwrap();
    // SAFETY: name is a valid C string within the fixture's private state directory.
    assert_eq!(unsafe { libc::mkfifo(name.as_ptr(), 0o600) }, 0);
    let _reader = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NONBLOCK)
        .open(&fallback)
        .unwrap();
    let out = s.launch("review").output().unwrap();
    s.expect(
        &out,
        2,
        "",
        "fleet-launch: fallback ledger unavailable; not starting\n",
    );
    assert!(!s.path("child.json").exists());
}

fn interrupted(launcher: &Launcher, rust: bool, phase: &str) {
    let s = Sandbox::new(launcher);
    let gate = s.path(&format!("hold-{phase}"));
    let entered = s.path(&format!("entered-{phase}"));
    fs::write(&gate, "").unwrap();
    fs::write(s.path("child.stdout"), "backgrounded no-id\n").unwrap();
    let mut child = s
        .direct_launch("review", rust)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .env("FLEET_RESOLVE_BUDGET", "15")
        .spawn()
        .unwrap();
    let deadline = Instant::now() + Duration::from_secs(5);
    while !entered.exists() && Instant::now() < deadline {
        thread::sleep(Duration::from_millis(5));
    }
    let observed = entered.exists();
    if child.try_wait().unwrap().is_none() {
        child.kill().unwrap();
    }
    fs::remove_file(gate).unwrap();
    let out = child.wait_with_output().unwrap();
    assert!(observed, "{phase}: {}", s.normalize(&out.stderr));
    assert_eq!(out.status.signal(), Some(libc::SIGKILL));
    assert!(out.stdout.is_empty(), "{}", s.normalize(&out.stdout));
    assert!(out.stderr.is_empty(), "{}", s.normalize(&out.stderr));
    let fallback = ledger(&s, "fallback");
    assert_eq!(fallback.len(), 1);
    assert_eq!(fallback[0]["result"], "starting");
    assert_eq!(fallback[0]["session_name"], "review-0001");
    assert_eq!(
        fs::read_to_string(s.path("state/session-seq")).unwrap(),
        "1"
    );
    if phase == "background" {
        assert!(!s.path("state/fleet-launch-ledger.jsonl").exists());
    } else {
        let started = ledger(&s, "ledger");
        assert_eq!(started.len(), 1);
        assert_eq!(started[0]["result"], "started");
        assert_eq!(started[0]["session_name"], "review-0001");
    }
    for path in ["state/launch", "guard", "oom"] {
        assert!(
            !s.path(path).exists(),
            "{path} was written before identity resolution"
        );
    }
}
