use pg_worker_core::{
    cargo_cache::try_exclusive_lease,
    disk_hygiene::{
        rank_candidates, reclaim, Budget, Candidate, Device, HygieneInventory, HygieneReport,
        KeptPath, ReclaimChecks, Thresholds,
    },
    host_link::{DISK_HYGIENE_BELOW_ALARM, DISK_HYGIENE_ZERO_PROGRESS},
};
use serde_json::Value;
use std::{
    cell::{Cell, RefCell},
    ffi::OsString,
    fs, io,
    os::unix::{ffi::OsStringExt, fs::MetadataExt},
    path::Path,
    time::{Duration, Instant, SystemTime},
};

fn artifact(root: &Path, name: &str, bytes: usize, copies: usize, cutoff: SystemTime) -> Candidate {
    let path = root.join(name);
    fs::create_dir(&path).unwrap();
    for index in 0..copies {
        let data = path.join(format!("data-{index}"));
        fs::write(&data, vec![0u8; bytes]).unwrap();
        age(&data, cutoff);
    }
    age(&path, cutoff);
    Candidate::new(path, root.to_path_buf(), cutoff).unwrap()
}

fn age(path: &Path, cutoff: SystemTime) {
    fs::File::open(path)
        .unwrap()
        .set_modified(cutoff - Duration::from_secs(3600))
        .unwrap();
}

fn pressure_case(initial: u64, copies: usize, young: bool, expired: bool) -> HygieneReport {
    let tmp = tempfile::tempdir().unwrap();
    let root = tmp.path().canonicalize().unwrap();
    let cutoff = SystemTime::now();
    let candidates = [
        ("small", 8192, 1),
        ("large", 65536, copies),
        ("middle", 32768, copies),
    ]
    .map(|(name, size, count)| artifact(&root, name, size, count, cutoff));
    let paths = candidates.each_ref().map(|item| item.path.clone());
    if young {
        let entries = fs::read_dir(&paths[2]).unwrap();
        let last = entries.last().unwrap().unwrap().path();
        fs::File::open(last).unwrap().set_modified(cutoff).unwrap();
    }
    let samples = RefCell::new(Vec::new());
    let space = |_: &Path| {
        let present = paths.each_ref().map(|path| path.is_dir());
        samples.borrow_mut().push(present);
        Ok(if present[2] { 150 } else { 200 })
    };
    let mut report = HygieneReport::new(Duration::ZERO, false);
    let id = root.metadata().unwrap().dev();
    let mut device = Device::new(id, root.clone(), Some(initial), Thresholds::default());
    device.cleanup_floor_bytes = 200;
    report.devices.insert(id, device);
    let start = Instant::now();
    let checked = Cell::new(false);
    let clock = || {
        if initial == 200
            && root.join("middle.plangraph-lock").exists()
            && try_exclusive_lease(&paths[2]).unwrap().is_none()
        {
            assert!(try_exclusive_lease(&paths[0]).unwrap().is_some());
            checked.set(true);
        }
        start
    };
    let mut budget = Budget::new(start, &clock);
    budget.candidate_entries = 3;
    if initial == 200 {
        budget.slice_entries = 1;
        let rows = rank_candidates(candidates.to_vec(), &budget);
        let ordered = rows.iter().map(|(item, _)| &item.path);
        assert!(ordered.eq([&paths[1], &paths[2], &paths[0]]));
        assert!(rows
            .iter()
            .all(|(_, row)| row.complete() && row.estimate().unwrap() > 0));
        assert!(checked.get());
    }
    if expired {
        budget.first_for = Duration::ZERO;
        budget.pass_for = Duration::ZERO;
    }
    let probes = RefCell::new(vec![]);
    let inventory = |deadline| {
        assert!(!probes.borrow().is_empty() || paths.iter().all(|path| path.exists()));
        probes.borrow_mut().push(deadline);
        Ok(HygieneInventory::default())
    };
    assert!(reclaim(
        &mut report,
        candidates.into(),
        &budget,
        &ReclaimChecks {
            inventory: &inventory,
            protection: &|_, _| Ok(None),
            space: &space
        }
    ));
    let total = report.devices.values().next().unwrap().freed_bytes;
    if initial < 200 && !young && !expired {
        assert_eq!(
            *samples.borrow(),
            [[true, false, true], [true, false, false]]
        );
        assert_eq!(
            *probes.borrow(),
            [
                start + budget.rank_for,
                start + budget.first_for,
                start + budget.pass_for
            ]
        );
        assert!(total > 0 && paths[0].is_dir());
        assert!(report
            .kept
            .iter()
            .any(|row| row.path == paths[0] && row.reason == "floor_met"));
    } else if !young {
        assert!(samples.borrow().is_empty());
        assert_eq!(total, 0);
        assert!(paths.iter().all(|path| path.exists()));
    }
    report
}

#[test]
fn pressure_removes_largest_until_resampled_floor() {
    pressure_case(150, 1, false, false);
    pressure_case(200, 1, false, false);
}

#[test]
fn first_removal_fits_budget_before_diagnostics() {
    let report = pressure_case(150, 3, false, false);
    assert!(report.time_to_first_removal.unwrap() <= 60.0);
    assert!(!report.first_removal_deadline_missed);
    assert!(!report.stage_durations.contains_key("diagnostics"));
}

#[test]
fn over_cap_idle_candidate_is_removed() {
    let idle = pressure_case(150, 3, false, false);
    assert_eq!(idle.summary.actions.len(), 2);
    let young = pressure_case(150, 3, true, false);
    assert!(young
        .kept
        .iter()
        .any(|row| row.path.ends_with("middle") && row.reason == "young"));
    let expired = pressure_case(150, 3, false, true);
    assert!(expired.kept.iter().any(|row| row.reason == "run_budget"
        && row.bytes_is_lower_bound
        && row.bytes.unwrap_or(0) > 0));
}

#[test]
fn ranking_orders_allocated_bytes_and_releases_each_slice() {
    pressure_case(200, 1, false, false);
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
                let sizes = kept.iter().map(|row| row["bytes"].as_u64().unwrap());
                assert!(sizes.eq([6, 5, 4, 3, 2]));
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

#[path = "../../../tests/support/disk_hygiene.rs"]
mod support;
#[test]
fn rebuildable_classes_keep_only_protected_paths() {
    use pg_worker_core::{
        cargo_pool::pool_root,
        disk_hygiene::Layout,
        disk_runtime::DiskSpace,
        private_targets::{fixture_stopped_guard, seed_cargo_fixture},
    };
    use serde_json::json;
    use support::{fixture, git};
    let (tmp, repo, trees) = fixture();
    let home = tmp.path();
    let path = |name| home.join(name);
    let removed = [
        "trees/clean/rust/target",
        "trees/dirty/rust/target",
        "main/rust/target",
        ".cache/session/target",
        ".cache/cargo-target/legacy",
        "scratch/cargo",
    ];
    let kept = [
        ("trees/pinned/rust/target", "young"),
        ("trees/live/rust/target", "unsafe"),
        (".cache/leased", "leased"),
        (".cache/mount", "unsafe"),
        (".cache/foreign", "unsafe"),
        (".cache/source", "unsafe"),
        (".cache/late", "busy"),
        ("scratch/unknown", "unrecognized"),
    ];
    let old = SystemTime::now() - Duration::from_secs(86400);
    let protected = kept.iter().map(|(name, _)| *name);
    for name in removed.into_iter().chain(protected) {
        let modified = old - Duration::from_secs(3600);
        let marked = !name.ends_with("unknown");
        seed_cargo_fixture(&path(name), modified, marked).unwrap();
    }
    let debug = trees.join("pinned/rust/target/debug");
    let stale =
        "alias.age=!GIT_COMMITTER_DATE=2000-01-01T00:00:00Z git commit --allow-empty -qm aged";
    git(&trees.join("pinned"), &["-c", stale, "age"]);
    fs::write(debug.join("young"), "new data").unwrap();
    age(&debug, old);
    git(
        &trees.join("live"),
        &["add", "-f", "rust/target/.rustc_info.json"],
    );
    let source = path(".cache/source");
    git(&source, &["init", "-q"]);
    age(&source, old);
    let _lease = try_exclusive_lease(&path(".cache/leased"))
        .unwrap()
        .unwrap();
    let guards = fixture_stopped_guard(home, &trees.join("clean")).unwrap();
    let shared = [path("primary"), path(".cache/secondary")];
    let slot = shared
        .each_ref()
        .map(|root| pool_root(root).unwrap().join("slot-1"));
    for root in &slot {
        super::tests::slot(root);
    }
    let scratch = [path("scratch")];
    let cache = path(".cache");
    let layout = Layout {
        repo: &repo,
        roots: &[trees],
        scratch: &scratch,
        shared: &shared,
        home,
        cache: Some(&cache),
        cap: 1024,
    };
    let start = Instant::now();
    let clock = || start;
    let budget = Budget::new(start, &clock);
    let mut report = HygieneReport::new(Duration::ZERO, false);
    let sample = |_: &Path| {
        Ok(DiskSpace {
            free: 0,
            total: 100,
        })
    };
    let limits = Thresholds::default();
    let found = layout.discover(&mut report, &budget, limits, sample, None);
    let first = Cell::new(true);
    let fd = path(".cache/late/.rustc_info.json")
        .into_os_string()
        .into_encoded_bytes();
    let late = json!({"version":1,"cwds":[],"cargo_targets":[],"open_paths":[fd]});
    let inventory = |_| match first.replace(false) {
        true => Ok(HygieneInventory::default()),
        false => serde_json::from_value(late.clone()).map_err(io::Error::other),
    };
    let protection = |item: &Candidate, end| {
        if item.path.ends_with("mount") || item.path.ends_with("foreign") {
            return Ok(Some("unsafe"));
        }
        if guards.protects(&item.scope) {
            return Ok(Some("busy"));
        }
        layout.protection(item, home, end)
    };
    let checks = ReclaimChecks {
        inventory: &inventory,
        protection: &protection,
        space: &|_| Ok(0),
    };
    assert!(reclaim(&mut report, found.unwrap(), &budget, &checks));
    assert!(removed.into_iter().all(|name| !path(name).exists()));
    assert!(scratch[0].is_dir() && slot.iter().all(|path| path.is_dir()));
    assert!(!slot[0].join("debug").exists() && slot[1].join("debug").is_dir());
    for (name, reason) in kept {
        let path = path(name);
        let row = report.kept.iter().find(|row| row.path == path);
        assert_eq!(row.map(|row| row.reason), Some(reason), "{name}");
        assert!(path.exists());
    }
}
