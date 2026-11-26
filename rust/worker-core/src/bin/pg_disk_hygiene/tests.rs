use pg_worker_core::{
    disk_hygiene::{Device, HygieneReport, KeptPath, Thresholds},
    host_link::{DISK_HYGIENE_BELOW_ALARM, DISK_HYGIENE_ZERO_PROGRESS},
};
use serde_json::Value;
use std::{ffi::OsString, fs, io, os::unix::ffi::OsStringExt, path::Path, time::Duration};

#[test]
fn final_proof_keeps_its_lease_and_restarts_after_metadata_changes() {
    use pg_worker_core::{
        cargo_cache::try_exclusive_lease,
        disk_hygiene::{Candidate, FinalProof},
    };
    use std::{cell::Cell, time::SystemTime};

    for changed in [false, true] {
        let tmp = tempfile::tempdir().unwrap();
        let root = tmp.path().canonicalize().unwrap();
        let target = root.join("target");
        let file = target.join("data");
        let now = SystemTime::now();
        fs::create_dir(&target).unwrap();
        fs::write(&file, [0u8; 8192]).unwrap();
        for path in [&target, &file] {
            fs::File::open(path)
                .unwrap()
                .set_modified(now - Duration::from_secs(3600))
                .unwrap();
        }
        let mut proof =
            FinalProof::begin(Candidate::new(target.clone(), root, now).unwrap()).unwrap();
        let steps = Cell::new(2usize);
        assert!(!proof
            .advance(|| {
                let previous = steps.get();
                steps.set(previous.saturating_sub(1));
                previous > 0
            })
            .unwrap());
        assert!(try_exclusive_lease(&target).unwrap().is_none());
        assert!(proof.remove(true, || Ok(true), || true).is_err());
        if changed {
            fs::File::open(&file).unwrap().set_modified(now).unwrap();
            assert_eq!(proof.advance(|| true), Err("young"));
            assert!(target.exists());
        } else {
            assert!(proof.advance(|| true).unwrap());
            assert_eq!(proof.remove(true, || Ok(false), || true).unwrap(), None);
            assert!(target.exists());
            assert!(proof.remove(true, || Ok(true), || true).unwrap().unwrap() > 0);
            assert!(!target.exists());
        }
    }
}

#[test]
fn ranking_orders_allocated_bytes_and_releases_each_slice() {
    use pg_worker_core::{
        cargo_cache::try_exclusive_lease,
        disk_hygiene::{rank_candidates, Budget, Candidate},
    };
    use std::{
        cell::Cell,
        time::{Instant, SystemTime},
    };

    let tmp = tempfile::tempdir().unwrap();
    let root = tmp.path().canonicalize().unwrap();
    let now = SystemTime::now();
    let old = now - Duration::from_secs(3600);
    let mut candidates = Vec::new();
    for (name, bytes) in [("small", 8192), ("large", 65536), ("middle", 32768)] {
        let path = root.join(name);
        fs::create_dir(&path).unwrap();
        fs::write(path.join("data"), vec![0; bytes]).unwrap();
        for file in [path.join("data"), path.clone()] {
            fs::File::open(file).unwrap().set_modified(old).unwrap();
        }
        candidates.push(Candidate::new(path, root.clone(), now).unwrap());
    }
    let start = Instant::now();
    let visits = Cell::new(0);
    let clock = || {
        if root.join("middle.plangraph-lock").exists()
            && try_exclusive_lease(&root.join("middle")).unwrap().is_none()
        {
            // The unselected slot must be available while another slot is being scanned.
            assert!(try_exclusive_lease(&root.join("small")).unwrap().is_some());
            visits.set(visits.get() + 1);
        }
        start
    };
    let mut budget = Budget::new(start, &clock);
    budget.slice_entries = 1;
    let rows = rank_candidates(candidates, &budget);
    assert_eq!(
        rows.iter()
            .map(|(item, _)| item.path.clone())
            .collect::<Vec<_>>(),
        ["large", "middle", "small"].map(|name| root.join(name))
    );
    assert!(rows
        .iter()
        .all(|(_, row)| row.complete() && row.estimate().unwrap() > 0));
    assert!(visits.get() > 0);
}

#[test]
fn truncated_ranking_refreshes_and_restarts_changed_metadata() {
    use pg_worker_core::disk_hygiene::{rank_candidates, refresh_ranking, Budget, Candidate};
    use std::time::{Instant, SystemTime};

    let tmp = tempfile::tempdir().unwrap();
    let root = tmp.path().canonicalize().unwrap();
    let now = SystemTime::now();
    let old = now - Duration::from_secs(3600);
    let mut candidates = vec![];
    for name in ["stable", "changed"] {
        let path = root.join(name);
        fs::create_dir(&path).unwrap();
        fs::write(path.join("data"), [0u8; 8192]).unwrap();
        for entry in [&path, &path.join("data")] {
            fs::File::open(entry).unwrap().set_modified(old).unwrap();
        }
        candidates.push(Candidate::new(path, root.clone(), now).unwrap());
    }
    let mut budget = Budget::new(Instant::now(), &Instant::now);
    budget.candidate_entries = 2;
    let mut rows = rank_candidates(candidates, &budget);
    assert!(rows.iter().all(|(_, row)| !row.complete()));
    fs::File::open(root.join("changed/data"))
        .unwrap()
        .set_modified(now)
        .unwrap();
    refresh_ranking(&mut rows, &budget);
    let stable = &rows
        .iter()
        .find(|(item, _)| item.path.ends_with("stable"))
        .unwrap()
        .1;
    let changed = &rows
        .iter()
        .find(|(item, _)| item.path.ends_with("changed"))
        .unwrap()
        .1;
    assert!(stable.complete());
    assert_eq!(changed.reason, Some("young"));
}

fn sample(root: &Path, end: [u64; 2], freed: [u64; 2], dry: bool) -> HygieneReport {
    let mut report = HygieneReport::new(Duration::new(100, 123), dry);
    let limits = Thresholds {
        cleanup_free_bytes: 20,
        alarm_free_bytes: 25,
    };
    for (index, device) in [10, 12].into_iter().enumerate() {
        let path = root.join(format!("device-{device}"));
        let mut row = Device::new(device, path, Some([11, 23][index]), limits);
        row.freed_bytes = freed[index];
        report.devices.insert(device, row);
        report.sample_end(device, Ok(end[index]), 111 + index as u64);
    }
    report
}

fn events(root: &Path) -> Vec<(String, Value)> {
    fs::read_dir(root.join("watchdog/outbox"))
        .into_iter()
        .flatten()
        .map(|entry| {
            let entry = entry.unwrap();
            (
                entry.file_name().into_string().unwrap(),
                serde_json::from_slice(&fs::read(entry.path()).unwrap()).unwrap(),
            )
        })
        .collect()
}

fn host() -> io::Result<String> {
    Ok("fixture-worker".into())
}

#[test]
fn partial_progress_below_alarm_floor_enqueues_and_reports_end_sample() {
    let fixture = tempfile::tempdir().unwrap();
    let state = fixture.path();
    let mut report = sample(state, [7, 22], [0, 9], false);
    for count in 1..=6 {
        let name = if count == 6 {
            OsString::from_vec(b"candidate-6%\xff".to_vec())
        } else {
            format!("candidate-{count}").into()
        };
        report.kept.push(KeptPath {
            device: Some(10),
            path: state.join(name),
            bytes: Some(count),
            bytes_is_lower_bound: count == 6,
            reason: "busy",
        });
    }
    let (output, queued) = report.persist(state, host);
    queued.unwrap();
    let output = output.unwrap();
    assert_eq!(fs::read(state.join("disk-hygiene.json")).unwrap(), output);
    assert_eq!(
        fs::read(state.join("disk-hygiene.ndjson")).unwrap(),
        [&output[..], b"\n"].concat()
    );
    let row: Value = serde_json::from_slice(&output).unwrap();
    assert_eq!(row["free_bytes"], 7);
    assert_eq!(row["freed_bytes"], 9);
    assert_eq!(row["alarm"], true);
    assert_eq!(row["error"], "disk hygiene zero progress");
    assert_eq!(row["devices"][0]["start_free_bytes"], 11);
    assert_eq!(row["devices"][1]["start_free_bytes"], 23);
    assert_eq!(row["devices"][1]["end_free_bytes"], 22);
    assert_eq!(row["kept"].as_array().unwrap().len(), 7);

    let queued = events(state);
    assert_eq!(queued.len(), 2);
    for (name, event) in queued {
        let id = event["event_id"].as_str().unwrap();
        let rule = event["rule"].as_str().unwrap();
        assert_eq!(id.len(), 64);
        assert_eq!(name, format!("{:020}-{rule}-{id}.json", 100));
        assert_eq!(event["kind"], "alert");
        assert_eq!(event["recipient"], "routing");
        assert_eq!(
            event["item_key"],
            "plangraph:LE-DISK-HYGIENE-DEFERS-ALL-CLEANUP-WHILE-ANY-BUILD-RUNS"
        );
        assert_eq!(event["host"], "fixture-worker");
        match rule {
            DISK_HYGIENE_ZERO_PROGRESS => {
                assert_eq!(event["device"], 10);
                assert_eq!(event["observed_at"], 111);
                assert_eq!(event["end_free_bytes"], 7);
                assert_eq!(event["floor_bytes"], 20);
                let kept = event["kept"].as_array().unwrap();
                assert_eq!(
                    kept.iter()
                        .map(|row| row["bytes"].as_u64().unwrap())
                        .collect::<Vec<_>>(),
                    [6, 5, 4, 3, 2]
                );
                assert_eq!(kept[0]["bytes_is_lower_bound"], true);
                assert!(kept[0]["path"]
                    .as_str()
                    .unwrap()
                    .ends_with("candidate-6%25%FF"));
            }
            DISK_HYGIENE_BELOW_ALARM => {
                assert_eq!(event["device"], 12);
                assert_eq!(event["observed_at"], 112);
                assert_eq!(event["floor_bytes"], 25);
                assert_eq!(event["freed_bytes"], 9);
                assert_eq!(event["kept"][0]["reason"], "no_candidates");
            }
            _ => panic!("unexpected rule {rule}"),
        }
    }
    for (end, freed, dry, expected) in [
        ([20, 25], [0, 9], false, 0),
        ([20, 24], [0, 9], false, 1),
        ([7, 22], [0, 0], true, 0),
    ] {
        let control = tempfile::tempdir().unwrap();
        let (output, queued) =
            sample(control.path(), end, freed, dry).persist(control.path(), host);
        queued.unwrap();
        let row: Value = serde_json::from_slice(&output.unwrap()).unwrap();
        assert_eq!(row["error"], Value::Null);
        assert_eq!(events(control.path()).len(), expected);
    }
}

#[test]
fn failed_final_samples_are_reported_without_pressure_events() {
    let fixture = tempfile::tempdir().unwrap();
    let state = fixture.path();
    let mut report = sample(state, [7, 22], [0, 0], false);
    for device in [10, 12] {
        report.sample_end(device, Err(io::Error::other("fixture sample failed")), 120);
    }
    let (report, queued) = report.persist(state, host);
    queued.unwrap();
    let row: Value = serde_json::from_slice(&report.unwrap()).unwrap();
    assert_eq!(row["free_bytes"], Value::Null);
    assert_eq!(row["alarm"], true);
    assert!(row["error"]
        .as_str()
        .unwrap()
        .contains("fixture sample failed"));
    assert!(events(state).is_empty());
}

#[test]
fn pressure_alerts_and_report_persistence_are_independent() {
    let fixture = tempfile::tempdir().unwrap();
    let state = fixture.path();
    fs::create_dir(state.join("disk-hygiene.json")).unwrap();
    let (report, queued) = sample(state, [7, 26], [0, 0], false).persist(state, host);
    assert!(report.is_err());
    queued.unwrap();
    assert_eq!(events(state).len(), 1);

    let fixture = tempfile::tempdir().unwrap();
    let state = fixture.path();
    fs::create_dir(state.join("watchdog")).unwrap();
    fs::write(state.join("watchdog/outbox"), b"blocked").unwrap();
    let (report, queued) = sample(state, [7, 26], [0, 0], false).persist(state, host);
    assert!(queued.is_err());
    let row: Value = serde_json::from_slice(&report.unwrap()).unwrap();
    assert_eq!(row["error"], "disk hygiene zero progress");
}
