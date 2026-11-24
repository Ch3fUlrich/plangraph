use super::*;

fn write_claim(state: &Path, key: &str, owner: &str, ts: u64, phase: &str) {
    let target = claim_path(state, key, owner);
    fs::create_dir_all(target.parent().unwrap()).unwrap();
    let record = json!({"key":key,"owner":owner,"ts":ts,"phase":phase});
    fs::write(target, record.to_string()).unwrap();
}

#[test]
fn a_reservation_holds_for_other_owners_until_it_is_abandoned() {
    let dir = tempfile::tempdir().unwrap();
    let keys = ["demo:K".to_owned()];
    reserve(dir.path(), &keys, "launch-a", now_secs()).unwrap();
    let at = now_secs();
    assert!(lanes(dir.path(), &keys, "launch-a", at).is_empty());
    let held = lanes(dir.path(), &keys, "launch-b", at);
    assert_eq!(held[0].name, "reservation launch-a");
    assert_eq!(held[0].source, "reservation");
    assert!(lanes(dir.path(), &keys, "launch-b", at + STARTED_SECS + 1).is_empty());
    assert!(lanes(dir.path(), &["demo:OTHER".to_owned()], "launch-b", at).is_empty());
}

#[test]
fn a_started_claim_survives_the_exit_of_the_dispatcher_and_an_admitted_one_does_not() {
    // The dispatcher died. If its supervisor was started it can still register the run: a
    // restarted dispatcher meets the claim until it is old enough to be abandoned. Before
    // the supervisor starts nothing can register, so the claim is a short crash backstop.
    let dir = tempfile::tempdir().unwrap();
    let keys = ["demo:K".to_owned()];
    let at = now_secs();
    write_claim(dir.path(), "demo:K", "launch-started", at - 300, "started");
    write_claim(
        dir.path(),
        "demo:K",
        "launch-admitted",
        at - 300,
        "admitted",
    );
    let names: Vec<_> = lanes(dir.path(), &keys, "launch-b", at)
        .into_iter()
        .map(|lane| lane.name)
        .collect();
    assert_eq!(names, ["reservation launch-started"]);
    assert!(lanes(dir.path(), &keys, "launch-b", at + 301).is_empty());
    let fresh = ["demo:F".to_owned()];
    reserve(dir.path(), &fresh, "launch-a", now_secs()).unwrap();
    assert_eq!(
        lanes(
            dir.path(),
            &fresh,
            "launch-b",
            now_secs() + PENDING_SECS - 1
        )
        .len(),
        1
    );
    assert!(lanes(
        dir.path(),
        &fresh,
        "launch-b",
        now_secs() + PENDING_SECS + 1
    )
    .is_empty());
    start(dir.path(), &fresh, "launch-a").unwrap();
    assert_eq!(
        lanes(
            dir.path(),
            &fresh,
            "launch-b",
            now_secs() + PENDING_SECS + 1
        )
        .len(),
        1
    );
}

#[test]
fn an_unfinished_write_is_not_a_claim_and_a_failed_publication_leaves_nothing() {
    let dir = tempfile::tempdir().unwrap();
    let keys = ["demo:K".to_owned()];
    let stray = key_dir(dir.path(), "demo:K");
    fs::create_dir_all(&stray).unwrap();
    let record = json!({"key":"demo:K","owner":"launch-a","ts":now_secs()}).to_string();
    fs::write(stray.join("0123.4567.tmp"), &record).unwrap();
    assert!(lanes(dir.path(), &keys, "launch-b", now_secs()).is_empty());
    // The rename cannot replace a directory: the temporary file must not outlive the failure.
    fs::create_dir_all(claim_path(dir.path(), "demo:K", "launch-b")).unwrap();
    assert!(reserve(dir.path(), &keys, "launch-b", now_secs()).is_err());
    let own = format!("{}.tmp", std::process::id());
    let leftovers: Vec<_> = fs::read_dir(&stray)
        .unwrap()
        .filter_map(Result::ok)
        .map(|entry| entry.file_name().to_string_lossy().into_owned())
        .filter(|name| name.ends_with(&own))
        .collect();
    assert!(leftovers.is_empty(), "{leftovers:?}");
}

#[test]
fn a_second_owner_never_replaces_the_first_claim() {
    let dir = tempfile::tempdir().unwrap();
    let keys = ["demo:K".to_owned()];
    reserve(dir.path(), &keys, "launch-a", now_secs()).unwrap();
    reserve(dir.path(), &keys, "launch-b", now_secs()).unwrap();
    assert_eq!(claim_files(dir.path()), 2);
    release(dir.path(), &keys, "launch-b");
    let held = lanes(dir.path(), &keys, "launch-c", now_secs());
    assert_eq!(held.len(), 1);
    assert_eq!(held[0].name, "reservation launch-a");
    release(dir.path(), &keys, "launch-a");
    prune(dir.path(), &keys);
    assert_eq!(claim_files(dir.path()), 0);
    assert!(!key_dir(dir.path(), "demo:K").exists());
    assert!(lanes(dir.path(), &keys, "launch-c", now_secs()).is_empty());
}

#[test]
fn start_refuses_an_expired_or_missing_claim_and_never_revives_it() {
    let dir = tempfile::tempdir().unwrap();
    let keys = ["demo:K".to_owned()];
    let at = now_secs();
    write_claim(dir.path(), "demo:K", "launch-a", at, "admitted");
    start(dir.path(), &keys, "launch-a").unwrap();
    // Started once: a second start keeps it started.
    start(dir.path(), &keys, "launch-a").unwrap();
    assert_eq!(
        lanes(dir.path(), &keys, "launch-b", at + PENDING_SECS + 1).len(),
        1
    );
    // A dispatcher paused past the 120 s deadline: another admission may have taken the key.
    write_claim(
        dir.path(),
        "demo:K",
        "launch-old",
        at - PENDING_SECS - 1,
        "admitted",
    );
    let error = start(dir.path(), &keys, "launch-old").unwrap_err();
    assert!(
        error.contains("expired before the launch started"),
        "{error}"
    );
    assert!(
        lanes(dir.path(), &keys, "launch-b", now_secs() + PENDING_SECS + 1)
            .iter()
            .all(|lane| lane.name != "reservation launch-old")
    );
    // A claim that is gone cannot be started either.
    assert!(start(dir.path(), &keys, "launch-gone").is_err());
}

#[cfg(unix)]
#[test]
fn sweep_reports_a_lapsed_claim_it_cannot_remove_and_removes_only_lapsed_ones() {
    use std::os::unix::fs::PermissionsExt;
    let dir = tempfile::tempdir().unwrap();
    let keys = ["demo:K".to_owned()];
    let at = now_secs();
    write_claim(
        dir.path(),
        "demo:K",
        "launch-lapsed",
        at - PENDING_SECS - 1,
        "admitted",
    );
    write_claim(dir.path(), "demo:K", "launch-live", at, "admitted");
    let claim_dir = key_dir(dir.path(), "demo:K");
    fs::set_permissions(&claim_dir, fs::Permissions::from_mode(0o555)).unwrap();
    let removable = fs::File::create(claim_dir.join("probe")).is_ok();
    let stuck = Snapshot::take(dir.path(), &keys, at).sweep();
    fs::set_permissions(&claim_dir, fs::Permissions::from_mode(0o755)).unwrap();
    if !removable {
        assert!(
            matches!(stuck, Err(SweepError::Remove(_))),
            "a claim left in place must not be reported swept"
        );
        assert_eq!(claim_files(dir.path()), 2);
    }
    let _ = fs::remove_file(claim_dir.join("probe"));
    Snapshot::take(dir.path(), &keys, at).sweep().unwrap();
    assert_eq!(claim_files(dir.path()), 1);
    let held = lanes(dir.path(), &keys, "launch-b", at);
    assert_eq!(held[0].name, "reservation launch-live");
}
