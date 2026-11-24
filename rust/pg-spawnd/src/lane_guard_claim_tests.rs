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
        error.contains("lane_unreadable: unreadable claim 0123456789abcdef.json"),
        "{error}"
    );
    // A deliberate second lane does not turn an unreadable claim into a free key.
    let mut second = request(dir.path(), &keys);
    second.explicit = Some("on purpose");
    let error = admit_launch(&second).unwrap_err();
    assert!(error.contains("an override does not apply"), "{error}");
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
        error.contains("lane_unreadable: unreadable claim"),
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
        error.contains("lane_unreadable: unreadable run record run-held"),
        "{error}"
    );
    fs::set_permissions(&record, fs::Permissions::from_mode(0o644)).unwrap();
    assert!(admit_launch(&request(dir.path(), &keys)).is_ok());
}

/// Every way of asking for the key: reserving, with an override, and check-only.
fn refusals(state: &Path, keys: &[String]) -> Vec<String> {
    let reserving = request(state, keys);
    let mut overridden = request(state, keys);
    overridden.explicit = Some("on purpose");
    let mut check = request(state, keys);
    check.reserve = false;
    let mut check_overridden = request(state, keys);
    check_overridden.reserve = false;
    check_overridden.explicit = Some("on purpose");
    [reserving, overridden, check, check_overridden]
        .iter()
        .map(|asked| match admit_launch(asked) {
            Ok(_) => "ADMITTED".to_owned(),
            Err(error) => error,
        })
        .collect()
}

#[test]
fn a_claim_that_is_readable_but_invalid_holds_the_key_against_every_request() {
    let key = "demo:INVALID";
    let keys = [key.to_owned()];
    let at = now_secs();
    let good = |owner: &str| json!({"key":key,"owner":owner,"ts":at,"phase":"admitted"});
    let variants: Vec<(&str, Value, Option<&str>)> = vec![
        ("missing key", json!({"owner":"launch-x","ts":at}), None),
        (
            "other key",
            json!({"key":"demo:OTHER","owner":"launch-x","ts":at}),
            None,
        ),
        ("empty owner", json!({"key":key,"owner":"","ts":at}), None),
        (
            "owner not a string",
            json!({"key":key,"owner":7,"ts":at}),
            None,
        ),
        (
            "timestamp not a number",
            json!({"key":key,"owner":"launch-x","ts":"now"}),
            None,
        ),
        (
            "unknown phase",
            json!({"key":key,"owner":"launch-x","ts":at,"phase":"done"}),
            None,
        ),
        ("file of another owner", good("launch-x"), Some("launch-y")),
    ];
    for (what, record, file_owner) in variants {
        let dir = tempfile::tempdir().unwrap();
        let target =
            crate::lane_reservation::claim_path(dir.path(), key, file_owner.unwrap_or("launch-x"));
        fs::create_dir_all(target.parent().unwrap()).unwrap();
        fs::write(&target, record.to_string()).unwrap();
        for error in refusals(dir.path(), &keys) {
            assert!(error.contains("unreadable claim"), "{what}: {error}");
        }
        assert_eq!(
            reservation_files(dir.path()),
            1,
            "{what}: nothing was published"
        );
    }
    // A valid claim of another owner still admits through the override only.
    let dir = tempfile::tempdir().unwrap();
    let target = crate::lane_reservation::claim_path(dir.path(), key, "launch-x");
    fs::create_dir_all(target.parent().unwrap()).unwrap();
    fs::write(&target, good("launch-x").to_string()).unwrap();
    let results = refusals(dir.path(), &keys);
    assert!(results[0].contains("lane_collision"), "{results:?}");
    assert_eq!(results[1], "ADMITTED", "{results:?}");
}

#[test]
fn a_run_record_that_is_unreadable_or_invalid_refuses_every_request_before_any_override() {
    let dir = tempfile::tempdir().unwrap();
    let keys = ["demo:FREE".to_owned()];
    let run = dir.path().join("runs/run-bad");
    fs::create_dir_all(&run).unwrap();
    for broken in ["{ not json", "[1, 2]", "\"text\"", ""] {
        fs::write(run.join("record.json"), broken).unwrap();
        for error in refusals(dir.path(), &keys) {
            assert!(
                error.contains("unreadable run record run-bad"),
                "{broken:?}: {error}"
            );
        }
    }
    // A readable record of a finished run holds nothing; the key is free again.
    fs::write(
        run.join("record.json"),
        json!({"run_id":"run-bad","status":"completed"}).to_string(),
    )
    .unwrap();
    assert!(refusals(dir.path(), &keys)
        .iter()
        .all(|result| result == "ADMITTED"));
}
