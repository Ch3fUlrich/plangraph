use super::{record::*, record_io::*};
use serde_json::{json, Map, Value};
use std::{collections::HashSet, fs, io, time::Duration};
use tempfile::tempdir;

const STATUSES: [Status; 11] = [
    Status::Queued,
    Status::Running,
    Status::Completed,
    Status::Failed,
    Status::Cancelled,
    Status::Stalled,
    Status::Timeout,
    Status::Refused,
    Status::Limited,
    Status::DryRun,
    Status::Orphaned,
];

#[test]
fn ids_and_paths() {
    for id in ["run-abc123", "run-0", "run-AZ09"] {
        assert!(valid_run_id(id));
    }
    for id in [
        "run-",
        "run-a_b",
        "run-a/b",
        "../run-a",
        "x",
        "run-é",
        &format!("run-{}", "a".repeat(65)),
    ] {
        assert!(!valid_run_id(id));
        assert!(matches!(
            run_dir(std::path::Path::new("state"), id),
            Err(RecordError::BadRunId)
        ));
    }
    assert!(valid_run_id(&format!("run-{}", "a".repeat(64))));
    for (prefix, length, generator) in [
        ("run-", 32, new_run_id as fn() -> String),
        ("job-", 8, new_job_id),
    ] {
        let mut seen = HashSet::new();
        for _ in 0..100 {
            let id = generator();
            let suffix = id.strip_prefix(prefix).unwrap();
            assert_eq!(suffix.len(), length);
            assert!(suffix
                .bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b)));
            assert!(seen.insert(id));
        }
    }
}

#[test]
fn defaults_and_overrides() {
    let dir = tempdir().unwrap();
    let r = RunRecord::create(
        dir.path(),
        "run-a",
        "fixture",
        Map::new(),
        Status::Queued,
        12.5,
    )
    .unwrap();
    assert_eq!(r.path(), dir.path().join("runs/run-a/record.json"));
    assert_eq!(
        r.to_value(),
        json!({"run_id":"run-a", "status":"queued", "started":12.5,
        "ended":null, "exit_code":null, "host":"fixture", "wait_cmd":"plangraph-spawn wait run-a",
        "attempts":[], "events":[], "stdout_tail":"", "stderr_tail":"", "served_model":"unwitnessed",
        "served_family":"unwitnessed", "served_witness_reason":"no authenticated harness telemetry; skipped or unobserved leg"})
    );
    assert_eq!(
        RunRecord::read(dir.path(), "run-a").unwrap().to_value(),
        r.to_value()
    );
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        for (p, mode) in [
            (r.path().to_path_buf(), 0o600),
            (dir.path().join("runs"), 0o700),
            (dir.path().join("runs/run-a"), 0o700),
        ] {
            assert_eq!(fs::metadata(p).unwrap().permissions().mode() & 0o777, mode);
        }
    }
    let base = json!({"host":"override", "started":2, "ended":3, "events":[{"event":"old"}], "run_id":"wrong", "status":"wrong"}).as_object().unwrap().clone();
    let r = RunRecord::create(
        dir.path(),
        "run-b",
        "fixture",
        base.clone(),
        Status::Running,
        8.0,
    )
    .unwrap();
    for key in ["host", "started", "ended", "events"] {
        assert_eq!(r.get(key), base.get(key));
    }
    assert_eq!(r.get("run_id"), Some(&json!("run-b")));
    assert_eq!(r.status(), Some(Status::Running));
    for status in STATUSES.into_iter().filter(|s| s.is_terminal()) {
        let r =
            RunRecord::create(dir.path(), "run-c", "fixture", base.clone(), status, 8.0).unwrap();
        assert_eq!(r.get("ended"), Some(&json!(8.0)));
    }
}

#[test]
fn transition_table_and_sticky_end() {
    use Status::*;
    let dir = tempdir().unwrap();
    let mut allowed = 0;
    for from in STATUSES {
        assert_eq!(Status::parse(from.as_str()), Some(from));
        assert_eq!(from.is_terminal(), !matches!(from, Queued | Running));
        for to in STATUSES {
            let mut r =
                RunRecord::create(dir.path(), "run-a", "fixture", Map::new(), from, 1.0).unwrap();
            let expected = matches!(
                (from, to),
                (Queued, Running | Cancelled | Failed | Refused)
                    | (
                        Running,
                        Completed | Failed | Cancelled | Stalled | Timeout | Orphaned
                    )
            );
            let result = r.transition(to, 2.0);
            if expected {
                allowed += 1;
                result.unwrap();
                assert_eq!(r.status(), Some(to));
                assert_eq!(
                    RunRecord::read(dir.path(), "run-a").unwrap().status(),
                    Some(from)
                );
                if to.is_terminal() {
                    assert_eq!(r.get("ended"), Some(&json!(2.0)));
                    assert!(matches!(
                        r.transition(Running, 3.0),
                        Err(RecordError::Terminal)
                    ));
                    assert_eq!(r.get("ended"), Some(&json!(2.0)));
                } else {
                    assert_eq!(r.get("ended"), Some(&Value::Null));
                }
            } else if from.is_terminal() {
                assert!(matches!(result, Err(RecordError::Terminal)));
                assert_eq!(r.get("ended"), Some(&json!(1.0)));
            } else {
                assert!(matches!(result, Err(RecordError::BadTransition)));
            }
        }
    }
    assert_eq!(allowed, 10);
    assert_eq!(Status::parse("dry_run"), Some(DryRun));
    assert_eq!(Status::parse("unknown"), None);
}

#[test]
fn failures_and_events() {
    let dir = tempdir().unwrap();
    for code in [Value::Null, json!(0)] {
        let mut r = RunRecord::create(
            dir.path(),
            "run-a",
            "fixture",
            Map::new(),
            Status::Queued,
            1.0,
        )
        .unwrap();
        r.set("exit_code", code);
        r.transition(Status::Failed, 2.0).unwrap();
        assert_eq!(r.get("exit_code"), Some(&json!(1)));
    }
    for (code, expected) in [
        (Value::Null, json!(1)),
        (json!(0), json!(1)),
        (json!(124), json!(124)),
        (json!(-9), json!(-9)),
        (json!(3), json!(3)),
    ] {
        let mut r = RunRecord::create(
            dir.path(),
            "run-a",
            "fixture",
            Map::new(),
            Status::Running,
            1.0,
        )
        .unwrap();
        r.set("exit_code", code);
        r.transition(Status::Failed, 2.0).unwrap();
        assert_eq!(r.get("exit_code"), Some(&expected));
        for event in [json!(0), json!({}), json!({"event":4})] {
            assert!(matches!(r.push_event(event), Err(RecordError::BadEvent)));
        }
        r.push_event(json!({"event":"first"})).unwrap();
        r.push_event(json!({"event":"second"})).unwrap();
        assert_eq!(
            r.get("events"),
            Some(&json!([{"event":"first"},{"event":"second"}]))
        );
        r.set("status", json!("future"));
        assert_eq!(r.status(), None);
    }
}

#[test]
fn orphaned_transition_terminal_and_exit_null() {
    let dir = tempdir().unwrap();
    let mut record = RunRecord::create(
        dir.path(),
        "run-recovery",
        "fixture",
        Map::new(),
        Status::Running,
        1.0,
    )
    .unwrap();
    record.set("exit_code", json!(1));
    record.transition(Status::Orphaned, 2.0).unwrap();
    record.save().unwrap();
    let mut record = RunRecord::read(dir.path(), "run-recovery").unwrap();
    assert_eq!(record.status(), Some(Status::Orphaned));
    assert_eq!(record.get("ended"), Some(&json!(2.0)));
    assert_eq!(record.get("exit_code"), Some(&Value::Null));
    for status in STATUSES {
        assert!(matches!(
            record.transition(status, 3.0),
            Err(RecordError::Terminal)
        ));
    }
    assert_eq!(record.get("ended"), Some(&json!(2.0)));
}

#[test]
fn python_fixture_and_errors() {
    let dir = tempdir().unwrap();
    assert!(matches!(
        RunRecord::read(dir.path(), "run-a"),
        Err(RecordError::Unknown)
    ));
    assert_eq!(RecordError::Unknown.to_string(), "unknown run id");
    let path = dir.path().join("fixture.json");
    let fixture = r#"{"status":"running","completion":{"sent":true},"watchdog":{"ticks":2},"pid_start":"fake","survivors":[42],"events":[],"custom":[null,{"nested":true}]}"#;
    fs::write(&path, fixture).unwrap();
    let original: Value = serde_json::from_str(fixture).unwrap();
    let mut r = RunRecord::read_path(&path).unwrap();
    r.set("stdout_tail", json!("changed"));
    r.save().unwrap();
    let reread = RunRecord::read_path(&path).unwrap();
    for (key, value) in original.as_object().unwrap() {
        assert_eq!(reread.get(key), Some(value));
    }
    for invalid in ["{", "[]", "null", "3"] {
        fs::write(&path, invalid).unwrap();
        assert!(matches!(
            RunRecord::read_path(&path),
            Err(RecordError::Invalid)
        ));
    }
}

#[test]
fn atomic_saves() {
    let dir = tempdir().unwrap();
    let mut r = RunRecord::create(
        dir.path(),
        "run-a",
        "fixture",
        Map::new(),
        Status::Running,
        1.0,
    )
    .unwrap();
    let path = r.path().to_path_buf();
    let done = std::sync::atomic::AtomicBool::new(false);
    std::thread::scope(|scope| {
        let reader = scope.spawn(|| {
            let mut reads = 0;
            while !done.load(std::sync::atomic::Ordering::Acquire) {
                if let Ok(text) = read_retry(&path) {
                    let v: Value = serde_json::from_str(&text).unwrap();
                    assert_eq!(v["run_id"], "run-a");
                    reads += 1;
                }
            }
            reads
        });
        for n in 0..300 {
            r.set("stdout_tail", json!("x".repeat(n)));
            r.save().unwrap();
        }
        done.store(true, std::sync::atomic::Ordering::Release);
        assert!(reader.join().unwrap() > 0);
    });
    assert_eq!(fs::read_dir(path.parent().unwrap()).unwrap().count(), 1);
}

#[test]
fn append_and_cleanup() {
    let dir = tempdir().unwrap();
    let path = dir.path().join("nested/log");
    append_private(&path, b"a").unwrap();
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(&path, fs::Permissions::from_mode(0o644)).unwrap();
    }
    append_private(&path, b"b").unwrap();
    assert_eq!(read_retry(&path).unwrap(), "ab");
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        assert_eq!(
            fs::metadata(&path).unwrap().permissions().mode() & 0o777,
            0o600
        );
        let target = dir.path().join("target");
        fs::create_dir(&target).unwrap();
        fs::write(target.join("child"), b"keep").unwrap();
        assert!(atomic_write_private(&target, b"data").is_err());
        assert_eq!(fs::read_dir(dir.path()).unwrap().count(), 2);
    }
}

#[test]
fn bounded_retries() {
    let mut calls = 0;
    let mut sleeps = Vec::new();
    let result: io::Result<()> = retry(
        24,
        || {
            calls += 1;
            Err(io::Error::other(calls.to_string()))
        },
        |_| true,
        |a| sleeps.push(a),
    );
    assert_eq!(calls, 24);
    assert_eq!(result.unwrap_err().to_string(), "24");
    assert_eq!(sleeps, (0..23).collect::<Vec<_>>());
    calls = 0;
    let _: io::Result<()> = retry(
        24,
        || {
            calls += 1;
            Err(io::Error::other("stop"))
        },
        |_| false,
        |_| panic!("must not sleep"),
    );
    assert_eq!(calls, 1);
    for (attempt, millis) in [(0, 10), (1, 20), (19, 200), (100, 200)] {
        assert_eq!(retry_delay(attempt), Duration::from_millis(millis));
    }
}
