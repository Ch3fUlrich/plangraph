//! The launch admission: claims, their phases and their release, through `admit_launch`.

use super::*;
use serde_json::json;

pub(super) fn request<'a>(state: &'a Path, extra: &'a [String]) -> LaunchRequest<'a> {
    LaunchRequest {
        prompt: "Brief\n",
        extra,
        state,
        repo: Path::new("/nonexistent"),
        explicit: None,
        reserve: true,
    }
}

pub(super) fn reservation_files(state: &Path) -> usize {
    crate::lane_reservation::claim_files(state)
}

#[test]
fn refused_launch_releases_reservation() {
    let dir = tempfile::tempdir().unwrap();
    let keys = ["demo:A".to_owned(), "demo:B".to_owned()];
    let first = admit_launch(&request(dir.path(), &keys)).unwrap();
    assert_eq!(reservation_files(dir.path()), 2);
    // A second launch is refused naming the reservation and reserves nothing new.
    let error = admit_launch(&request(dir.path(), &keys[1..])).unwrap_err();
    assert!(error.contains("held by live lane reservation"), "{error}");
    assert_eq!(reservation_files(dir.path()), 2);
    // The first launch fails after admission: dropping it frees the keys at once.
    drop(first);
    assert_eq!(reservation_files(dir.path()), 0);
    drop(admit_launch(&request(dir.path(), &keys[1..])).unwrap());
    assert_eq!(reservation_files(dir.path()), 0);
}

#[test]
fn a_check_only_request_reserves_nothing() {
    let dir = tempfile::tempdir().unwrap();
    let keys = ["demo:A".to_owned()];
    let mut check = request(dir.path(), &keys);
    check.reserve = false;
    let launched = admit_launch(&check).unwrap();
    assert_eq!(launched.keys, keys);
    assert_eq!(reservation_files(dir.path()), 0);
    assert!(admit_launch(&request(dir.path(), &keys)).is_ok());
}

#[test]
fn two_concurrent_admissions_of_one_key_admit_one() {
    let dir = tempfile::tempdir().unwrap();
    let keys = ["demo:RACE".to_owned()];
    let barrier = std::sync::Barrier::new(8);
    let results: Vec<Result<Launched, String>> = std::thread::scope(|scope| {
        let workers: Vec<_> = (0..8)
            .map(|_| {
                scope.spawn(|| {
                    barrier.wait();
                    admit_launch(&request(dir.path(), &keys))
                })
            })
            .collect();
        workers.into_iter().map(|w| w.join().unwrap()).collect()
    });
    assert_eq!(
        results.iter().filter(|r| r.is_ok()).count(),
        1,
        "{results:?}"
    );
    for refused in results.iter().filter_map(|r| r.as_ref().err()) {
        assert!(
            refused.contains("demo:RACE is held by live lane"),
            "{refused}"
        );
    }
}

#[test]
fn override_with_reason_is_recorded_and_blank_is_refused() {
    let dir = tempfile::tempdir().unwrap();
    let keys = ["demo:HELD".to_owned()];
    let first = admit_launch(&request(dir.path(), &keys)).unwrap();
    let mut second = request(dir.path(), &keys);
    second.explicit = Some("  fix lane  ");
    let launched = admit_launch(&second).unwrap();
    assert_eq!(launched.lane_override.as_deref(), Some("fix lane"));
    assert_eq!(launched.keys, keys);
    for blank in ["", "  \t"] {
        second.explicit = Some(blank);
        let error = admit_launch(&second).unwrap_err();
        assert_eq!(error, "lane_override requires a nonblank reason");
    }
    drop((first, launched));
}

#[test]
fn dropping_an_override_launch_leaves_the_original_claim() {
    let dir = tempfile::tempdir().unwrap();
    let keys = ["demo:HELD".to_owned()];
    let first = admit_launch(&request(dir.path(), &keys)).unwrap();
    let mut second = request(dir.path(), &keys);
    second.explicit = Some("on purpose");
    let override_launch = admit_launch(&second).unwrap();
    assert_eq!(reservation_files(dir.path()), 2);
    // The override fails before it registers: the first launch is still pending and holds.
    drop(override_launch);
    assert_eq!(reservation_files(dir.path()), 1);
    let error = admit_launch(&request(dir.path(), &keys)).unwrap_err();
    assert!(error.contains("held by live lane reservation"), "{error}");
    drop(first);
    assert_eq!(reservation_files(dir.path()), 0);
}

pub(super) fn run_holding(state: &Path, key: &str) {
    let run = state.join("runs/run-held");
    fs::create_dir_all(&run).unwrap();
    fs::write(
        run.join("record.json"),
        json!({"run_id":"run-held","status":"running","role":"implement","item_key":key})
            .to_string(),
    )
    .unwrap();
}

fn now_secs() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |d| d.as_secs())
}

/// Drops `launched` on another thread; the channel says when the drop has returned.
fn drop_in_thread(launched: Launched) -> std::sync::mpsc::Receiver<()> {
    let (done, finished) = std::sync::mpsc::channel();
    std::thread::spawn(move || {
        drop(launched);
        done.send(()).unwrap();
    });
    finished
}

#[test]
fn a_release_waits_for_an_admission_in_progress() {
    // Dispatch B holds the state lock while it checks the runs and the claims. Launch A's release
    // must wait for it, so that the claim is still visible at the time B looks for it.
    let dir = tempfile::tempdir().unwrap();
    let keys = ["demo:RACE".to_owned()];
    let first = admit_launch(&request(dir.path(), &keys)).unwrap();
    let admission = crate::lane_reservation::lock(dir.path()).unwrap();
    let finished = drop_in_thread(first);
    // The drop is blocked on the lock: it cannot return while B holds it.
    let early = finished.recv_timeout(std::time::Duration::from_millis(300));
    assert!(early.is_err(), "the release did not wait for the admission");
    let held = crate::lane_reservation::lanes(dir.path(), &keys, "dispatch-b", now_secs());
    assert_eq!(held.len(), 1, "the claim is visible to the admission");
    drop(admission);
    finished
        .recv_timeout(std::time::Duration::from_secs(10))
        .unwrap();
    assert_eq!(reservation_files(dir.path()), 0);
}

#[test]
fn a_finished_launch_frees_its_claim_although_another_admission_holds_the_lock() {
    // A competing admission that outlasts the release wait must not leave a started claim for the
    // rest of its ten-minute lease.
    let dir = tempfile::tempdir().unwrap();
    let keys = ["demo:BUSY".to_owned()];
    let first = admit_launch(&request(dir.path(), &keys)).unwrap();
    first.mark_started().unwrap();
    let admission = crate::lane_reservation::lock(dir.path()).unwrap();
    let finished = drop_in_thread(first);
    finished
        .recv_timeout(std::time::Duration::from_secs(10))
        .expect("the release gave up with the lock still held");
    assert_eq!(reservation_files(dir.path()), 0);
    drop(admission);
    assert!(admit_launch(&request(dir.path(), &keys)).is_ok());
}

#[test]
fn an_admission_reads_the_claims_before_the_runs() {
    // Launch A registers its run and releases its claim between B's two reads. B read no claim
    // yet or already: either way it must find A, as a claim or as a run.
    let dir = tempfile::tempdir().unwrap();
    let keys = ["demo:ORDER".to_owned()];
    let first = std::cell::RefCell::new(Some(admit_launch(&request(dir.path(), &keys)).unwrap()));
    let observed = Observed {
        state: dir.path().to_owned(),
        repo: PathBuf::from("/nonexistent"),
        owner: "dispatch-b".into(),
    };
    let lanes = observed.lanes_between(&keys, &|| {
        run_holding(dir.path(), "demo:ORDER");
        first.borrow_mut().take();
    });
    assert_eq!(reservation_files(dir.path()), 0);
    let found: Vec<_> = lanes.iter().map(|lane| lane.source).collect();
    assert!(
        found.contains(&"reservation") && found.contains(&"run"),
        "{found:?}"
    );
}

#[test]
fn a_launch_whose_claim_expired_or_vanished_cannot_start() {
    // A dispatcher paused past the deadline of its pending claim: another admission may hold the
    // key by now, so the launch is refused rather than reviving the claim.
    let dir = tempfile::tempdir().unwrap();
    let keys = ["demo:PAUSED".to_owned()];
    let launched = admit_launch(&request(dir.path(), &keys)).unwrap();
    let claim = fs::read_dir(dir.path().join("lane-reservations"))
        .unwrap()
        .filter_map(Result::ok)
        .filter(|entry| entry.path().is_dir())
        .flat_map(|entry| fs::read_dir(entry.path()).unwrap())
        .filter_map(Result::ok)
        .map(|entry| entry.path())
        .find(|path| path.extension().is_some_and(|ext| ext == "json"))
        .unwrap();
    let mut record: Value = serde_json::from_slice(&fs::read(&claim).unwrap()).unwrap();
    record["ts"] = json!(now_secs() - crate::lane_reservation::PENDING_SECS - 1);
    fs::write(&claim, record.to_string()).unwrap();
    // Another dispatcher admits the key now that the pending claim has lapsed.
    let second = admit_launch(&request(dir.path(), &keys)).unwrap();
    let error = launched.mark_started().unwrap_err();
    assert!(
        error.contains("expired before the launch started"),
        "{error}"
    );
    // The refused launch's drop leaves the second dispatcher's claim alone.
    drop(launched);
    assert_eq!(reservation_files(dir.path()), 1);
    assert!(admit_launch(&request(dir.path(), &keys)).is_err());
    second.mark_started().unwrap();
}

#[test]
fn a_claim_is_marked_started_for_the_supervisor_and_a_check_has_nothing_to_mark() {
    let dir = tempfile::tempdir().unwrap();
    let keys = ["demo:START".to_owned()];
    let launched = admit_launch(&request(dir.path(), &keys)).unwrap();
    let at = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |d| d.as_secs());
    let after = at + crate::lane_reservation::PENDING_SECS + 1;
    let held = |at| crate::lane_reservation::lanes(dir.path(), &keys, "dispatch-b", at).len();
    assert_eq!(held(at), 1);
    assert_eq!(
        held(after),
        0,
        "an admitted claim is a short crash backstop"
    );
    launched.mark_started().unwrap();
    assert_eq!(held(after), 1, "a started claim waits for the supervisor");
    drop(launched);
    assert_eq!(held(at), 0);
    let mut check = request(dir.path(), &keys);
    check.reserve = false;
    admit_launch(&check).unwrap().mark_started().unwrap();
    assert_eq!(reservation_files(dir.path()), 0);
}
