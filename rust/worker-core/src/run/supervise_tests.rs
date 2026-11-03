use super::{
    capture::{Capture, CaptureCfg, Stream},
    finalize,
    group::{LaunchError, StopOutcome},
    record::{RunRecord, Status},
    scrub::Scrubber,
    supervise::*,
};
use serde_json::json;

fn spec() -> RunSpec {
    RunSpec {
        run_id: "run-test".into(),
        job_id: "job-test".into(),
        program: "unused".into(),
        args: vec![],
        cwd: Default::default(),
        env: vec![],
        stdin: None,
        base: Default::default(),
        events: vec![],
        finish: None,
    }
}
fn runtime(dir: &std::path::Path) -> std::sync::Arc<Runtime> {
    Runtime::new(Config {
        state_dir: dir.into(),
        ..Default::default()
    })
}
#[test]
fn validation_and_creation() {
    let d = tempfile::tempdir().unwrap();
    let rt = runtime(d.path());
    for key in [
        "status",
        "started",
        "ended",
        "exit_code",
        "pid",
        "pid_start",
        "job_object",
        "job_id",
        "proc",
        "last_activity_ts",
        "stdout_tail",
        "stderr_tail",
        "events",
        "live_log",
        "queued_at",
        "reason",
        "error",
        "survivors",
    ] {
        let mut s = spec();
        s.base.insert(key.into(), json!(null));
        assert!(matches!(create_record(&rt, &mut s), Err(StartError::Reserved(k)) if k == key));
    }
    for id in [
        "",
        "run-",
        "run-a/b",
        "run-é",
        &format!("run-{}", "x".repeat(65)),
    ] {
        let mut s = spec();
        s.run_id = id.into();
        assert!(matches!(create_record(&rt, &mut s), Err(StartError::BadId)));
    }
    for id in [
        "",
        "job-",
        "job-a_b",
        "job-é",
        &format!("job-{}", "x".repeat(33)),
    ] {
        let mut s = spec();
        s.job_id = id.into();
        assert!(matches!(create_record(&rt, &mut s), Err(StartError::BadId)));
    }
    for event in [json!(null), json!("event"), json!({}), json!({"event": 1})] {
        let mut s = spec();
        s.events.push(event);
        assert!(matches!(
            create_record(&rt, &mut s),
            Err(StartError::BadEvent)
        ));
    }
    assert_eq!(std::fs::read_dir(d.path()).unwrap().count(), 0);
    let mut s = spec();
    s.events.push(json!({"event":"initial"}));
    s.base.insert("role".into(), json!("test"));
    let r = create_record(&rt, &mut s).unwrap();
    let saved = RunRecord::read_path(r.path()).unwrap().to_value();
    assert_eq!(saved["status"], "queued");
    assert_eq!(saved["role"], "test");
    assert_eq!(saved["events"], json!([{"event":"initial"}]));
    assert!(saved["started"].as_f64().unwrap() > 0.0);
}
#[test]
fn outcomes_and_hook_order() {
    for (code, cancel, pids, expected, expected_code, reason, error) in [
        (0, false, vec![], Status::Completed, 0, None, None),
        (
            3,
            false,
            vec![],
            Status::Failed,
            3,
            Some("harness exit 3"),
            Some("harness exit code 3"),
        ),
        (
            -9,
            false,
            vec![],
            Status::Failed,
            -9,
            Some("harness exit -9"),
            Some("harness exit code -9"),
        ),
        (
            0,
            true,
            vec![],
            Status::Cancelled,
            1,
            Some("cancelled"),
            None,
        ),
        (
            0,
            true,
            vec![9, 2],
            Status::Failed,
            1,
            Some("kill_incomplete"),
            None,
        ),
        (
            -9,
            true,
            vec![9, 2],
            Status::Failed,
            -9,
            Some("kill_incomplete"),
            None,
        ),
        (
            3,
            false,
            vec![9, 2],
            Status::Failed,
            3,
            Some("kill_incomplete"),
            None,
        ),
    ] {
        let d = tempfile::tempdir().unwrap();
        let rt = runtime(d.path());
        let mut r = create_record(&rt, &mut spec()).unwrap();
        r.transition(Status::Running, finalize::now()).unwrap();
        let mut hook: Option<FinishHook> = Some(Box::new(move |r, status| {
            assert_eq!(status, expected);
            assert_eq!(r.status(), Some(Status::Running));
            assert_eq!(r.get("exit_code"), Some(&json!(expected_code)));
            r.set("hook", json!(true));
        }));
        let survivors = !pids.is_empty();
        finalize::finish(
            &mut r,
            &mut hook,
            code,
            cancel,
            if survivors {
                StopOutcome::Survivors(pids)
            } else {
                StopOutcome::Empty
            },
            None,
        );
        let v = RunRecord::read_path(r.path()).unwrap().to_value();
        assert_eq!(r.status(), Some(expected));
        assert_eq!(v["exit_code"], expected_code);
        assert_eq!(v["reason"].as_str(), reason);
        assert_eq!(v["error"].as_str(), error);
        assert_eq!(v["hook"], true);
        assert!(v["ended"].is_number());
        if survivors {
            assert_eq!(v["survivors"], json!([2, 9]));
            assert_eq!(
                v["events"],
                json!([{"event":"kill_incomplete","survivors":[2,9]}])
            );
        }
    }
}
#[test]
fn final_capture_and_panicking_hook() {
    let d = tempfile::tempdir().unwrap();
    let rt = runtime(d.path());
    let mut r = create_record(&rt, &mut spec()).unwrap();
    let c = Capture::new(CaptureCfg {
        live_log: None,
        live_cfg: Default::default(),
        scrubber: Scrubber::from_env(&[]),
    });
    c.feed(Stream::Stdout, b"out\n");
    c.feed(
        Stream::Stderr,
        format!("\n\x1b[31m  {}  \x1b[0m\nsecond\n", "z".repeat(310)).as_bytes(),
    );
    c.finish(Stream::Stdout);
    c.finish(Stream::Stderr);
    let mut hook: Option<FinishHook> = Some(Box::new(|_, _| panic!("test hook")));
    finalize::finish(&mut r, &mut hook, 3, false, StopOutcome::Empty, Some(&c));
    let v = r.to_value();
    assert_eq!(v["status"], "failed");
    assert_eq!(v["reason"], "z".repeat(300));
    assert_eq!(v["error"], "z".repeat(300));
    assert_eq!(v["stdout_tail"], "out\n");
    assert!(v["last_activity_ts"].is_number());
    assert_eq!(v["stderr_tail"], c.snapshot().stderr_final);
    assert_eq!(v["events"], json!([{"event":"finish_hook_failed"}]));
}
#[test]
fn launch_failure_table() {
    for (e, name, event) in [
        (
            LaunchError::Io(std::io::ErrorKind::NotFound.into()),
            "NotFound",
            "spawn_failed",
        ),
        (
            LaunchError::ScopeUnavailable("unavailable".into()),
            "ScopeUnavailable",
            "spawn_failed",
        ),
        (
            LaunchError::ScopeNotEntered,
            "ScopeNotEntered",
            "spawn_failed",
        ),
        (LaunchError::Unsupported, "Unsupported", "spawn_failed"),
        (
            LaunchError::JobUnavailable,
            "JobUnavailable",
            "win_job_unavailable",
        ),
        (LaunchError::JobExists, "JobExists", "win_job_unavailable"),
        (LaunchError::BadUnit, "BadUnit", "spawn_failed"),
    ] {
        let d = tempfile::tempdir().unwrap();
        let rt = runtime(d.path());
        let mut r = create_record(&rt, &mut spec()).unwrap();
        launch_error(&mut r, e);
        finalize::finish(&mut r, &mut None, 1, false, StopOutcome::Empty, None);
        let v = r.to_value();
        assert_eq!(v["status"], "failed");
        assert_eq!(v["exit_code"], 1);
        assert_eq!(v["error"], format!("spawn failed: {name}"));
        assert_eq!(v["reason"], v["error"]);
        let mut expected = vec![];
        expected.push(if event == "spawn_failed" {
            json!({"event":event,"error":name})
        } else {
            json!({"event":event})
        });
        if name == "ScopeUnavailable" {
            expected.push(json!({"event":"scope_unavailable","detail":"unavailable"}));
        }
        assert_eq!(v["events"], json!(expected));
        assert!(v.get("pid").is_none());
        assert!(v.get("live_log").is_none());
        assert!(v["ended"].is_number());
    }
}
#[test]
fn carried_low_probes() {
    for separator in [
        '\n', '\r', '\u{b}', '\u{c}', '\u{85}', '\u{2028}', '\u{2029}',
    ] {
        let secret = ["vvvv", "vvvvvv"].concat();
        let value = format!("aaaa\nxxxx{separator}{secret}");
        let scrub = Scrubber::from_env(&[("API_KEY".into(), value.into())]);
        assert!(!scrub.scrub(&secret).contains(&secret));
    }
    let c = Capture::new(CaptureCfg {
        live_log: None,
        live_cfg: Default::default(),
        scrubber: Scrubber::from_env(&[]),
    });
    c.feed(Stream::Stdout, b"\xff\xfe\n");
    c.finish(Stream::Stdout);
    assert_eq!(c.snapshot().stdout_final, "\u{fffd}\u{fffd}\n");
    assert_eq!(super::activity::strip_ansi("\x1b]a\x1b\\b\x07"), "b\x07");
    let unicode = "é".repeat(8);
    let ascii = "abcdefgh";
    let scrub = Scrubber::from_env(&[
        ("API_KEY".into(), unicode.clone().into()),
        ("TOKEN".into(), ascii.into()),
    ]);
    assert!(!scrub.scrub(&format!("{unicode} {ascii}")).contains(ascii));
    assert!(!scrub.scrub(&unicode).contains(&unicode));
}
