//! Claims in odd states: a clock that steps back, claims that cannot be removed or read.

use super::launch_tests::{request, reservation_files, run_holding};
use super::*;
use crate::lane_reservation::now_secs;
use serde_json::json;

#[test]
fn a_clock_that_steps_back_cannot_revive_a_claim_another_admission_replaced() {
    // A's pending claim lapses; B is admitted over it at the later time. When the clock then steps
    // back to a time at which A's claim would hold again, A must not start next to B.
    let dir = tempfile::tempdir().unwrap();
    let keys = ["demo:CLOCK".to_owned()];
    let first = admit_launch(&request(dir.path(), &keys)).unwrap();
    let claim = || {
        fs::read_dir(dir.path().join("lane-reservations"))
            .unwrap()
            .filter_map(Result::ok)
            .filter(|entry| entry.path().is_dir())
            .flat_map(|entry| fs::read_dir(entry.path()).unwrap())
            .filter_map(Result::ok)
            .map(|entry| entry.path())
            .find(|path| path.extension().is_some_and(|ext| ext == "json"))
    };
    let path = claim().unwrap();
    let mut record: Value = serde_json::from_slice(&fs::read(&path).unwrap()).unwrap();
    let written = record["ts"].clone();
    record["ts"] = json!(now_secs() - crate::lane_reservation::PENDING_SECS - 1);
    fs::write(&path, record.to_string()).unwrap();
    let second = admit_launch(&request(dir.path(), &keys)).unwrap();
    // The clock steps back: whatever is left of A's claim looks fresh again.
    if path.exists() {
        record["ts"] = written;
        fs::write(&path, record.to_string()).unwrap();
    }
    let error = first.mark_started().unwrap_err();
    assert!(
        error.contains("expired before the launch started"),
        "{error}"
    );
    drop(first);
    assert_eq!(reservation_files(dir.path()), 1);
    second.mark_started().unwrap();
}

#[test]
fn a_clock_that_steps_back_between_the_scan_and_the_sweep_cannot_leave_two_claims() {
    // A's pending claim lapsed at the time B's admission reads the clock once. The wall clock then
    // steps back (the scripted second reading): B must not read it again to decide about A's
    // claim, or it would leave the claim in place next to its own, and both could start.
    let dir = tempfile::tempdir().unwrap();
    let keys = ["demo:SCAN".to_owned()];
    let first = admit_launch(&request(dir.path(), &keys)).unwrap();
    let written = now_secs();
    let later = written + crate::lane_reservation::PENDING_SECS + 10;
    crate::lane_reservation::CLOCK.with(|clock| *clock.borrow_mut() = vec![later, written]);
    let second = admit_launch(&request(dir.path(), &keys));
    crate::lane_reservation::CLOCK.with(|clock| clock.borrow_mut().clear());
    let second = second.unwrap();
    let error = first.mark_started().unwrap_err();
    assert!(
        error.contains("expired before the launch started"),
        "{error}"
    );
    drop(first);
    assert_eq!(reservation_files(dir.path()), 1);
    second.mark_started().unwrap();
}

/// The key directory of the one claim a test made.
fn claim_dir(state: &Path) -> PathBuf {
    fs::read_dir(state.join("lane-reservations"))
        .unwrap()
        .filter_map(Result::ok)
        .map(|entry| entry.path())
        .find(|path| path.is_dir())
        .unwrap()
}

#[test]
fn a_lapsed_claim_whose_removal_fails_refuses_before_publishing_the_replacement() {
    // The sweep fails while publication would work: the refusal must come from the sweep and
    // nothing of the replacement may exist.
    let dir = tempfile::tempdir().unwrap();
    let keys = ["demo:SWEEP".to_owned()];
    let first = admit_launch(&request(dir.path(), &keys)).unwrap();
    let lapsed = now_secs() + crate::lane_reservation::PENDING_SECS + 10;
    crate::lane_reservation::CLOCK.with(|clock| *clock.borrow_mut() = vec![lapsed]);
    crate::lane_reservation::REMOVE_FAILS.with(|fails| fails.set(true));
    let second = admit_launch(&request(dir.path(), &keys));
    crate::lane_reservation::REMOVE_FAILS.with(|fails| fails.set(false));
    crate::lane_reservation::CLOCK.with(|clock| clock.borrow_mut().clear());
    let error = second.unwrap_err();
    assert!(error.contains("lapsed claim cannot be removed"), "{error}");
    assert!(!error.contains("cannot be published"), "{error}");
    assert_eq!(
        reservation_files(dir.path()),
        1,
        "only the first claim exists"
    );
    // Nobody took the key, so the first launch still holds it.
    first.mark_started().unwrap();
}

#[test]
fn an_unreadable_claim_holds_the_key_even_against_an_override() {
    let dir = tempfile::tempdir().unwrap();
    let keys = ["demo:BROKEN".to_owned()];
    let first = admit_launch(&request(dir.path(), &keys)).unwrap();
    let key_dir = claim_dir(dir.path());
    drop(first);
    fs::create_dir_all(&key_dir).unwrap();
    fs::write(key_dir.join("0123456789abcdef.json"), "{ not a claim").unwrap();
    let error = admit_launch(&request(dir.path(), &keys)).unwrap_err();
    assert!(
        error.contains("held by live lane unreadable claim 0123456789abcdef.json"),
        "{error}"
    );
    // A deliberate second lane does not turn an unreadable claim into a free key.
    let mut second = request(dir.path(), &keys);
    second.explicit = Some("on purpose");
    let error = admit_launch(&second).unwrap_err();
    assert!(error.contains("the key stays held"), "{error}");
    assert_eq!(
        reservation_files(dir.path()),
        1,
        "no replacement was published"
    );
    fs::remove_file(key_dir.join("0123456789abcdef.json")).unwrap();
    assert!(admit_launch(&request(dir.path(), &keys)).is_ok());
}

#[cfg(unix)]
#[test]
fn a_claim_or_run_record_that_cannot_be_read_holds_the_keys() {
    use std::os::unix::fs::PermissionsExt;
    let dir = tempfile::tempdir().unwrap();
    let keys = ["demo:DENIED".to_owned()];
    let first = admit_launch(&request(dir.path(), &keys)).unwrap();
    let claim = fs::read_dir(claim_dir(dir.path()))
        .unwrap()
        .filter_map(Result::ok)
        .map(|entry| entry.path())
        .find(|path| path.extension().is_some_and(|ext| ext == "json"))
        .unwrap();
    fs::set_permissions(&claim, fs::Permissions::from_mode(0o000)).unwrap();
    if fs::read(&claim).is_ok() {
        fs::set_permissions(&claim, fs::Permissions::from_mode(0o644)).unwrap();
        return; // a superuser ignores the permissions: nothing to observe
    }
    let error = admit_launch(&request(dir.path(), &keys)).unwrap_err();
    assert!(
        error.contains("held by live lane unreadable claim"),
        "{error}"
    );
    fs::set_permissions(&claim, fs::Permissions::from_mode(0o644)).unwrap();
    drop(first);
    // The same for a run record of the state: it may hold any key.
    run_holding(dir.path(), "demo:OTHER");
    let record = dir.path().join("runs/run-held/record.json");
    fs::set_permissions(&record, fs::Permissions::from_mode(0o000)).unwrap();
    let error = admit_launch(&request(dir.path(), &keys)).unwrap_err();
    assert!(
        error.contains("held by live lane unreadable run record run-held"),
        "{error}"
    );
    fs::set_permissions(&record, fs::Permissions::from_mode(0o644)).unwrap();
    assert!(admit_launch(&request(dir.path(), &keys)).is_ok());
}
