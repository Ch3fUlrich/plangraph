use pg_worker_core::fleet_series::{annotate, summarize, Sample};
use pg_worker_core::run::record::{RunRecord, Status};
use serde_json::json;

fn sample(t: u64, oom: u64, sessions: &[&str]) -> Sample {
    serde_json::from_value(json!({"host":"worker", "ts":t, "cpu_load":1.0,
        "mem_avail_mb":8000,"swap_used_mb":0,"psi_mem_avg10":0.0,"disk_pct":30.0,
        "cores":4,"runs":2,"ready":1,"sessions":sessions,"sessions_lost":[],
        "oom_kill":oom,"top_rss":[],"reachable":true}))
    .unwrap()
}

#[test]
fn loss_requires_adjacent_reachable_sample_and_oom_increase() {
    let old = sample(60, 3, &["one", "two"]);
    let mut now = sample(120, 4, &["two"]);
    annotate(&mut now, Some(&old));
    assert_eq!(now.sessions_lost, ["one"]);
    for (t, oom) in [(120, 3), (240, 4), (120, 1)] {
        let mut now = sample(t, oom, &[]);
        annotate(&mut now, Some(&old));
        assert!(now.sessions_lost.is_empty());
    }
}

#[test]
fn summary_counts_gaps_resets_and_idle_ready() {
    let mut rows = vec![
        sample(60, 7, &["one"]),
        sample(120, 9, &[]),
        sample(240, 1, &[]),
    ];
    let old = rows[0].clone();
    annotate(&mut rows[1], Some(&old));
    rows[1].runs = 0;
    rows[2].reachable = false;
    let report = summarize(&rows, 60, 300);
    assert_eq!(report["worker"].oom_kill_delta, 2);
    assert_eq!(report["worker"].sessions_lost, 1);
    assert_eq!(report["worker"].unreachable_minutes, 2);
    assert_eq!(report["worker"].runs_p90, 2);
    assert_eq!(report["worker"].runs_mean, 1.0);
    assert!((report["worker"].idle_with_ready_hours - 1.0 / 60.0).abs() < 0.0001);
}

#[test]
fn terminal_record_uses_its_stored_host() {
    let tmp = tempfile::tempdir().unwrap();
    std::fs::create_dir(tmp.path().join("fleet")).unwrap();
    let rows = [sample(60, 2, &[]), sample(120, 5, &[])];
    let text = rows
        .iter()
        .map(|r| serde_json::to_string(r).unwrap() + "\n")
        .collect::<String>();
    std::fs::write(tmp.path().join("fleet/worker.ndjson"), text).unwrap();
    let mut record = RunRecord::create(
        tmp.path(),
        "run-abc",
        "worker",
        Default::default(),
        Status::Running,
        60.0,
    )
    .unwrap();
    record.transition(Status::Completed, 120.0).unwrap();
    assert_eq!(record.get("oom_kill_delta"), Some(&json!(3)));
    let mut other = RunRecord::create(
        tmp.path(),
        "run-def",
        "other",
        Default::default(),
        Status::Running,
        60.0,
    )
    .unwrap();
    other.transition(Status::Completed, 120.0).unwrap();
    assert_eq!(other.get("oom_kill_delta"), Some(&json!(null)));
}

#[test]
fn append_bounds_history_and_keeps_oom_baseline() {
    use pg_worker_core::fleet_series::{append, read};
    let tmp = tempfile::tempdir().unwrap();
    let path = tmp.path().join("worker.ndjson");
    let text: String = (0..1441)
        .map(|i| serde_json::to_string(&sample(i * 60, 1, &["lost"])).unwrap() + "\n")
        .collect();
    std::fs::write(&path, text).unwrap();
    let mut now = sample(1441 * 60, 2, &[]);
    append(tmp.path(), &mut now).unwrap();
    let rows = read(&path).unwrap();
    assert_eq!(rows.len(), 1440);
    assert_eq!(rows[0].ts, 120);
    assert_eq!(rows.last().unwrap().ts, now.ts);
    assert_eq!(now.sessions_lost, ["lost"]);
}

#[test]
fn append_reads_only_bounded_tail_and_caps_bytes() {
    use pg_worker_core::fleet_series::{append, read};
    use std::io::Write;
    let tmp = tempfile::tempdir().unwrap();
    let path = tmp.path().join("worker.ndjson");
    let mut file = std::fs::File::create(&path).unwrap();
    file.write_all(&vec![b'x'; 5 * 1024 * 1024]).unwrap();
    writeln!(file).unwrap();
    let mut old = sample(60, 1, &["lost"]);
    old.sessions.insert("a".repeat(48 * 1024));
    for _ in 0..100 {
        writeln!(file, "{}", serde_json::to_string(&old).unwrap()).unwrap();
    }
    drop(file);
    let mut now = sample(120, 2, &[]);
    append(tmp.path(), &mut now).unwrap();
    assert!(std::fs::metadata(&path).unwrap().len() <= 4 * 1024 * 1024);
    assert_eq!(read(&path).unwrap().last().unwrap().ts, 120);
    assert!(now.sessions_lost.iter().any(|s| s == "lost"));
}

#[test]
fn rejected_append_preserves_history() {
    use pg_worker_core::fleet_series::{append, HISTORY_BYTES};
    let tmp = tempfile::tempdir().unwrap();
    let path = tmp.path().join("worker.ndjson");
    append(tmp.path(), &mut sample(60, 1, &[])).unwrap();
    let before = std::fs::read(&path).unwrap();
    let mut huge = sample(120, 2, &[]);
    huge.sessions.insert("x".repeat(HISTORY_BYTES));
    assert!(append(tmp.path(), &mut huge).is_err());
    assert_eq!(std::fs::read(&path).unwrap(), before);
    assert!(append(
        tmp.path(),
        &mut Sample {
            host: "../escape".into(),
            ..Sample::default()
        }
    )
    .is_err());
    let _guard = pg_worker_core::lockfile::try_lock(&tmp.path().join("worker.lock"))
        .unwrap()
        .unwrap();
    let result = append(tmp.path(), &mut sample(120, 2, &[]));
    assert_eq!(result.unwrap_err().kind(), std::io::ErrorKind::WouldBlock);
    assert_eq!(std::fs::read(&path).unwrap(), before);
}

#[test]
fn bounded_reads_keep_aligned_tail_and_report_corruption() {
    use pg_worker_core::fleet_series::{read, HISTORY_BYTES};
    let tmp = tempfile::tempdir().unwrap();
    let path = tmp.path().join("worker.ndjson");
    let mut row = sample(60, 1, &[]);
    let base = serde_json::to_vec(&row).unwrap().len();
    // Adding one session adds its quotes and contents to the empty array.
    row.sessions.insert("x".repeat(HISTORY_BYTES - base - 3));
    let bytes = serde_json::to_string(&row).unwrap() + "\n";
    assert_eq!(bytes.len(), HISTORY_BYTES);
    std::fs::write(&path, "obsolete\n".to_owned() + &bytes).unwrap();
    assert_eq!(read(&path).unwrap().len(), 1);
    std::fs::write(&path, "corrupt\n").unwrap();
    assert!(read(&path).is_err());
}

fn finish_run(state: &std::path::Path, started: f64, ended: f64) -> RunRecord {
    let mut record = RunRecord::create(
        state,
        "run-cov",
        "worker",
        Default::default(),
        Status::Running,
        started,
    )
    .unwrap();
    record.transition(Status::Completed, ended).unwrap();
    record
}

#[test]
fn one_sample_before_the_run_is_not_coverage_of_the_run() {
    let tmp = tempfile::tempdir().unwrap();
    std::fs::create_dir(tmp.path().join("fleet")).unwrap();
    let text = serde_json::to_string(&sample(60, 2, &[])).unwrap() + "\n";
    std::fs::write(tmp.path().join("fleet/worker.ndjson"), text).unwrap();
    let record = finish_run(tmp.path(), 61.0, 120.0);
    assert_eq!(record.get("oom_kill_delta"), Some(&json!(null)));
}

#[cfg(unix)]
#[test]
fn terminal_transition_does_not_block_on_a_fifo_history() {
    use std::os::unix::ffi::OsStrExt;
    let tmp = tempfile::tempdir().unwrap();
    std::fs::create_dir(tmp.path().join("fleet")).unwrap();
    let fifo = tmp.path().join("fleet/worker.ndjson");
    let c_path = std::ffi::CString::new(fifo.as_os_str().as_bytes()).unwrap();
    assert_eq!(unsafe { libc::mkfifo(c_path.as_ptr(), 0o600) }, 0);
    let state = tmp.path().to_path_buf();
    let (tx, rx) = std::sync::mpsc::channel();
    std::thread::spawn(move || {
        let record = finish_run(&state, 60.0, 120.0);
        let _ = tx.send(record.get("oom_kill_delta").cloned());
    });
    let delta = rx
        .recv_timeout(std::time::Duration::from_secs(10))
        .expect("terminal transition blocked on the history path");
    assert_eq!(delta, Some(json!(null)));
}

#[cfg(unix)]
#[test]
fn history_symlink_is_not_followed() {
    let tmp = tempfile::tempdir().unwrap();
    std::fs::create_dir(tmp.path().join("fleet")).unwrap();
    let real = tmp.path().join("elsewhere.ndjson");
    let text = [sample(60, 2, &[]), sample(120, 5, &[])]
        .iter()
        .map(|r| serde_json::to_string(r).unwrap() + "\n")
        .collect::<String>();
    std::fs::write(&real, text).unwrap();
    std::os::unix::fs::symlink(&real, tmp.path().join("fleet/worker.ndjson")).unwrap();
    assert!(pg_worker_core::fleet_series::read(&tmp.path().join("fleet/worker.ndjson")).is_err());
    let record = finish_run(tmp.path(), 60.0, 120.0);
    assert_eq!(record.get("oom_kill_delta"), Some(&json!(null)));
}
