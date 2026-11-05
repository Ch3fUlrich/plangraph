use super::testutil::{Count, Survivors};
use super::{group::*, record::RunRecord, supervise::*};
use serde_json::Value;
use std::{
    sync::{
        atomic::{AtomicBool, AtomicUsize, Ordering},
        Arc,
    },
    thread,
    time::{Duration, Instant},
};
fn setup(script: &str) -> (tempfile::TempDir, Arc<Runtime>, RunSpec) {
    let d = tempfile::tempdir().unwrap();
    let rt = Runtime::new(Config {
        state_dir: d.path().into(),
        confinement: Confinement::Plain,
        grace: Duration::from_millis(300),
        kill_timeout: Duration::from_secs(2),
        save_every: Duration::from_millis(100),
        reader_grace: Duration::from_secs(1),
        held_poll: Duration::from_millis(50),
        ..Default::default()
    });
    let s = RunSpec {
        run_id: "run-test".into(),
        job_id: "job-test".into(),
        program: "sh".into(),
        args: vec!["-c".into(), script.into()],
        cwd: d.path().into(),
        env: vec![],
        stdin: None,
        base: Default::default(),
        events: vec![],
        finish: None,
    };
    (d, rt, s)
}
fn read(rt: &Runtime) -> Value {
    RunRecord::read(&rt.cfg.state_dir, "run-test")
        .unwrap()
        .to_value()
}
fn wait(h: RunHandle) {
    let deadline = Instant::now() + Duration::from_secs(4);
    while !h.is_finished() {
        assert!(Instant::now() < deadline, "run exceeded deadline");
        thread::sleep(Duration::from_millis(10));
    }
    h.join().unwrap();
}
#[test]
fn completed_fields_and_log() {
    let (_d, rt, mut s) = setup("echo out; echo err >&2");
    s.finish = Some(Box::new(|r, status| {
        assert_eq!(status, super::record::Status::Completed);
        r.set("hook_status", serde_json::json!(status.as_str()));
    }));
    let h = start(&rt, s).unwrap();
    assert_eq!(h.run_id(), "run-test");
    wait(h);
    let v = read(&rt);
    assert_eq!(v["hook_status"], "completed");
    assert_eq!(v["status"], "completed");
    assert_eq!(v["exit_code"], 0);
    assert!(v["ended"].as_f64() >= v["started"].as_f64());
    assert!(v["started"].as_f64() >= v["queued_at"].as_f64());
    assert_eq!(v["stdout_tail"], "out\n");
    assert_eq!(v["stderr_tail"], "err\n");
    assert!(v["pid"].is_number());
    #[cfg(target_os = "linux")]
    assert!(v["pid_start"].is_number());
    assert_eq!(v["job_object"], false);
    assert_eq!(v["job_id"], "job-test");
    assert_eq!(v["proc"]["pid"], v["pid"]);
    let log = std::fs::read_to_string(v["live_log"].as_str().unwrap()).unwrap();
    assert!(log.contains("out\n") && log.contains("err\n"));
    assert!(v["last_activity_ts"].is_number());
    assert_eq!(v["events"], serde_json::json!([]));
}
#[test]
fn failure_and_signal() {
    for (script, code, reason, error) in [
        ("echo boom >&2; exit 3", 3, "boom", "boom"),
        ("exit 3", 3, "harness exit 3", "harness exit code 3"),
        ("kill -9 $$", -9, "harness exit -9", "harness exit code -9"),
    ] {
        let (_d, rt, s) = setup(script);
        wait(start(&rt, s).unwrap());
        let v = read(&rt);
        assert_eq!(v["status"], "failed");
        assert_eq!(v["exit_code"], code);
        assert_eq!(v["reason"], reason);
        assert_eq!(v["error"], error);
    }
}
#[test]
fn missing_program() {
    let (_d, rt, mut s) = setup("");
    s.program = "/nonexistent-harness".into();
    wait(start(&rt, s).unwrap());
    let v = read(&rt);
    assert_eq!(v["status"], "failed");
    assert_eq!(v["exit_code"], 1);
    assert_eq!(
        v["events"],
        serde_json::json!([{"event":"spawn_failed","error":"NotFound"}])
    );
    // finalize keeps the launch error it was handed (finalize.rs: reason already set).
    assert_eq!(v["error"], "spawn failed: NotFound");
    assert_eq!(v["reason"], v["error"]);
    for key in [
        "pid",
        "pid_start",
        "job_object",
        "job_id",
        "proc",
        "live_log",
    ] {
        assert!(v.get(key).is_none());
    }
    assert!(v["ended"].is_number());
    assert!(!rt.cfg.state_dir.join("runs/run-test/live.log").exists());
}
#[test]
fn stdin_and_complete_environment() {
    for payload in [None, Some(b"round trip\n".to_vec())] {
        let (_d, rt, mut s) = setup("cat");
        s.stdin = payload.clone();
        wait(start(&rt, s).unwrap());
        assert_eq!(
            read(&rt)["stdout_tail"],
            String::from_utf8(payload.unwrap_or_default()).unwrap()
        );
    }
    let (_d, rt, mut s) = setup("printf '%s:%s' \"$FOO\" \"${HOME-unset}\"");
    s.env = vec![("FOO".into(), "bar".into())];
    wait(start(&rt, s).unwrap());
    assert_eq!(read(&rt)["stdout_tail"], "bar:unset");
}
#[test]
fn periodic_save() {
    let (_d, rt, s) = setup("i=0; while [ $i -lt 10 ]; do echo tick; sleep .1; i=$((i+1)); done");
    let h = start(&rt, s).unwrap();
    let deadline = Instant::now() + Duration::from_secs(2);
    loop {
        let v = read(&rt);
        if v["status"] == "running" && !v["stdout_tail"].as_str().unwrap().is_empty() {
            assert!(v["last_activity_ts"].is_number());
            break;
        }
        assert!(Instant::now() < deadline);
        thread::sleep(Duration::from_millis(20));
    }
    wait(h);
}
#[test]
fn cancel_flag_and_marker() {
    for marker in [false, true] {
        let (_d, rt, s) = setup("trap '' TERM; sleep 30 & wait");
        let began = Instant::now();
        let h = start(&rt, s).unwrap();
        thread::sleep(Duration::from_millis(200));
        if marker {
            std::fs::write(rt.cfg.state_dir.join("runs/run-test/cancelled"), b"").unwrap();
        } else {
            h.cancel();
        }
        wait(h);
        let v = read(&rt);
        assert_eq!(v["status"], "cancelled");
        assert_eq!(v["exit_code"], 1);
        assert_eq!(v["reason"], "cancelled");
        assert!(began.elapsed() < Duration::from_secs(5));
        #[cfg(target_os = "linux")]
        {
            let pid = v["pid"].as_u64().unwrap();
            assert!(!std::path::Path::new(&format!("/proc/{pid}")).exists());
        }
    }
}
struct Block;
impl Admission for Block {
    fn acquire(&self, _: &str, c: &AtomicBool) -> Admit {
        while !c.load(Ordering::SeqCst) {
            thread::sleep(Duration::from_millis(10));
        }
        Admit::Cancelled
    }
}
#[test]
fn queued_cancel_returns_before_admission() {
    let (_d, rt, s) = setup("echo should-not-launch");
    let rt = Runtime::with(
        Arc::try_unwrap(rt).ok().unwrap().cfg,
        Arc::new(Block),
        Arc::new(SystemLauncher),
    );
    let h = start(&rt, s).unwrap();
    assert_eq!(read(&rt)["status"], "queued");
    h.cancel();
    wait(h);
    let v = read(&rt);
    assert_eq!(v["status"], "cancelled");
    assert!(v.get("pid").is_none());
    assert!(v.get("live_log").is_none());
}
#[test]
fn queued_marker_cancel() {
    let (_d, rt, s) = setup("echo should-not-launch");
    let rt = Runtime::with(
        Arc::try_unwrap(rt).ok().unwrap().cfg,
        Arc::new(Block),
        Arc::new(SystemLauncher),
    );
    let h = start(&rt, s).unwrap();
    std::fs::write(rt.cfg.state_dir.join("runs/run-test/cancelled"), b"").unwrap();
    wait(h);
    assert_eq!(read(&rt)["status"], "cancelled");
    assert!(read(&rt).get("pid").is_none());
}
#[test]
fn secret_never_persisted_and_long_output_bounded() {
    let (_d, rt, mut s) = setup("printf '%s\\n' \"$API_KEY\"");
    let secret = ["sensitive-", "runtime-value"].concat();
    s.env = vec![("API_KEY".into(), secret.clone().into())];
    wait(start(&rt, s).unwrap());
    let v = read(&rt);
    assert!(
        !std::fs::read_to_string(rt.cfg.state_dir.join("runs/run-test/record.json"))
            .unwrap()
            .contains(&secret)
    );
    assert!(!std::fs::read_to_string(v["live_log"].as_str().unwrap())
        .unwrap()
        .contains(&secret));
    let (_d, rt, s) = setup("head -c 200000 /dev/zero | tr '\\000' x");
    wait(start(&rt, s).unwrap());
    let v = read(&rt);
    let out = v["stdout_tail"].as_str().unwrap();
    assert!(out.len() <= 65536 + 40);
    assert!(out.contains("truncated"));
    assert!(v["stderr_tail"].as_str().unwrap().len() <= 4096);
}
#[test]
fn natural_exit_sweeps_grandchild() {
    let (_d, rt, s) = setup("sleep 30 & echo $!");
    let began = Instant::now();
    wait(start(&rt, s).unwrap());
    let v = read(&rt);
    assert_eq!(v["status"], "completed");
    assert!(began.elapsed() < Duration::from_secs(3));
    #[cfg(target_os = "linux")]
    {
        let pid = v["stdout_tail"].as_str().unwrap().trim();
        let stat = std::fs::read_to_string(format!("/proc/{pid}/stat"));
        assert!(stat.is_err() || stat.unwrap().split_whitespace().nth(2) == Some("Z"));
    }
}
struct Panics;
impl Launcher for Panics {
    fn launch(
        &self,
        _: &str,
        _: &LaunchSpec<'_>,
        _: &Confinement,
        _: &RunLimits,
    ) -> Result<Grouped, LaunchError> {
        panic!("fake launcher")
    }
}
#[test]
fn launcher_panic_is_terminal() {
    let (_d, rt, s) = setup("");
    let rt = Runtime::with(
        Arc::try_unwrap(rt).ok().unwrap().cfg,
        Arc::new(NoLimit),
        Arc::new(Panics),
    );
    wait(start(&rt, s).unwrap());
    let v = read(&rt);
    assert_eq!(v["status"], "failed");
    assert_eq!(v["exit_code"], 1);
    assert_eq!(
        v["events"],
        serde_json::json!([{"event":"monitor_failed","error":"panic"}])
    );
}
#[test]
fn survivors_hold_permit_until_empty() {
    let (_d, rt, s) = setup("exit 0");
    let count = Arc::new(AtomicUsize::new(0));
    let empty = Arc::new(AtomicBool::new(false));
    let rt = Runtime::with(
        Arc::try_unwrap(rt).ok().unwrap().cfg,
        Arc::new(Count(count.clone())),
        Arc::new(Survivors(empty.clone())),
    );
    let h = start(&rt, s).unwrap();
    let deadline = Instant::now() + Duration::from_secs(3);
    while read(&rt)["status"] != "failed" {
        assert!(Instant::now() < deadline);
        thread::sleep(Duration::from_millis(10));
    }
    let v = read(&rt);
    assert_eq!(v["reason"], "kill_incomplete");
    assert_eq!(v["survivors"], serde_json::json!([3, 7]));
    assert_eq!(
        v["events"],
        serde_json::json!([{"event":"kill_incomplete","survivors":[3,7]}])
    );
    assert_eq!(count.load(Ordering::SeqCst), 1);
    assert!(!h.is_finished());
    empty.store(true, Ordering::SeqCst);
    let began = Instant::now();
    wait(h);
    assert!(began.elapsed() < Duration::from_secs(2));
    assert_eq!(count.load(Ordering::SeqCst), 0);
}
#[cfg(target_os = "linux")]
#[test]
fn live_scope() {
    if !super::group_unix::scope_available("agent-tests.slice") {
        eprintln!("scope unavailable; skipping live scope test");
        return;
    }
    let (_d, rt, s) = setup("echo scope; sleep .2");
    let mut cfg = Arc::try_unwrap(rt).ok().unwrap().cfg;
    cfg.confinement = Confinement::Scope {
        slice: "agent-tests.slice".into(),
    };
    let rt = Runtime::new(cfg);
    wait(start(&rt, s).unwrap());
    let v = read(&rt);
    assert_eq!(v["status"], "completed");
    // R6a-2c enter_scope may answer Ok(None) for a scope that ended first (open issue).
    let group = v["proc"]["group"].as_str();
    assert!(group.is_none_or(|g| g.ends_with(".scope")), "{group:?}");
}
