//! The keeper claim: retry key and the cleanup of a partial grant.
use super::*;

const GRANTED: &str = r#"{"results":[{"key":"demo:A","epoch":3},{"key":"demo:B","epoch":9}]}"#;

#[test]
fn a_claim_killed_after_the_keeper_granted_it_retries_with_the_same_key() {
    let key_file = key_file();
    let mut first = Script::new(vec![Ok(GRANTED)]);
    KeeperCli {
        runner: &mut first,
        dir: ".".into(),
        prefix: vec!["pg".into()],
        key_file: key_file.clone(),
    }
    .claim_batch(&keys())
    .unwrap();
    // The process died before its row was saved. The next one is a new KeeperCli on the same
    // file: it must ask the keeper with the key of the first call, not a fresh one.
    let mut second = Script::new(vec![Ok(GRANTED)]);
    let held = KeeperCli {
        runner: &mut second,
        dir: ".".into(),
        prefix: vec!["pg".into()],
        key_file,
    }
    .claim_batch(&keys())
    .unwrap();
    assert_eq!(held.len(), 2);
    assert_eq!(first.seen[0][1], second.seen[0][1], "one key for one claim");
}

#[test]
fn a_refused_claim_settles_its_key_so_the_next_attempt_asks_anew() {
    let key_file = key_file();
    let refused = r#"{"results":[{"key":"demo:A","error":"claimed"}]}"#;
    let mut first = Script::new(vec![Ok(refused)]);
    let mut keeper = KeeperCli {
        runner: &mut first,
        dir: ".".into(),
        prefix: vec!["pg".into()],
        key_file: key_file.clone(),
    };
    assert!(keeper.claim_batch(&keys()).is_err());
    assert!(!key_file.exists(), "a replay of the refusal would be stale");
}

const MIXED: &str =
    r#"{"results":[{"key":"demo:A","epoch":3},{"key":"demo:B","error":"claimed"}]}"#;

fn keeper<'a>(script: &'a mut Script, key_file: &std::path::Path) -> KeeperCli<&'a mut Script> {
    KeeperCli {
        runner: script,
        dir: ".".into(),
        prefix: vec!["pg".into()],
        key_file: key_file.to_owned(),
    }
}

#[test]
fn a_release_whose_answer_was_lost_after_the_keeper_committed_it_is_confirmed() {
    let key_file = key_file();
    // The release is refused on replay and the item is open again: the grant is gone.
    let mut script = Script::new(vec![
        Ok(MIXED),
        Err("pg exit 2: demo:A is open, not claimed"),
        Ok(r#"{"key":"demo:A","state":"open","epoch":3}"#),
    ]);
    let error = keeper(&mut script, &key_file)
        .claim_batch(&keys())
        .unwrap_err();
    assert!(!error.contains("release failed"), "{error}");
    assert_eq!(script.seen[2], ["pg", "show", "demo:A", "--live"]);
    assert!(
        !key_file.exists(),
        "the cleanup is confirmed, so the key goes"
    );
}

#[test]
fn a_grant_that_the_keeper_still_holds_keeps_the_key() {
    let key_file = key_file();
    let mut script = Script::new(vec![
        Ok(MIXED),
        Err("pg: unreachable"),
        Ok(r#"{"key":"demo:A","state":"claimed","epoch":3}"#),
    ]);
    let error = keeper(&mut script, &key_file)
        .claim_batch(&keys())
        .unwrap_err();
    assert!(error.contains("release failed: demo:A@3"), "{error}");
    assert!(key_file.exists());
}

#[test]
fn a_release_is_confirmed_when_another_claim_holds_the_item_at_a_later_epoch() {
    let key_file = key_file();
    let mut script = Script::new(vec![
        Ok(MIXED),
        Err("pg exit 2: epoch mismatch"),
        Ok(r#"{"key":"demo:A","state":"claimed","epoch":4}"#),
    ]);
    let error = keeper(&mut script, &key_file)
        .claim_batch(&keys())
        .unwrap_err();
    assert!(!error.contains("release failed"), "{error}");
}

#[test]
fn a_kill_during_the_cleanup_of_a_partial_grant_keeps_the_key() {
    let key_file = key_file();
    let mixed = r#"{"results":[{"key":"demo:A","epoch":3},{"key":"demo:B","error":"claimed"}]}"#;
    // The release of demo:A does not finish (the process is killed, or the release fails).
    let mut first = Script::new(vec![Ok(mixed), Err("killed")]);
    let mut keeper = KeeperCli {
        runner: &mut first,
        dir: ".".into(),
        prefix: vec!["pg".into()],
        key_file: key_file.clone(),
    };
    assert!(keeper.claim_batch(&keys()).is_err());
    assert!(
        key_file.exists(),
        "the grant of demo:A is not yet given back"
    );
    // The next process asks with the same key, so the keeper replays the grant it holds.
    let mut second = Script::new(vec![Ok(mixed), Ok("{}")]);
    let mut again = KeeperCli {
        runner: &mut second,
        dir: ".".into(),
        prefix: vec!["pg".into()],
        key_file: key_file.clone(),
    };
    assert!(again.claim_batch(&keys()).is_err());
    assert_eq!(first.seen[0][1], second.seen[0][1]);
    assert!(!key_file.exists(), "dropped once the cleanup is confirmed");
}

#[test]
fn a_keeper_outage_during_the_cleanup_never_clears_the_key() {
    let key_file = key_file();
    // The release fails and so does the live read (`show --live` exits 3 when the keeper is
    // unreachable, where plain `show` would answer from the stale snapshot).
    let mut script = Script::new(vec![
        Ok(MIXED),
        Err("pg: keeper unreachable"),
        Err("pg exit 3: keeper unreachable"),
    ]);
    let error = keeper(&mut script, &key_file)
        .claim_batch(&keys())
        .unwrap_err();
    assert!(error.contains("release failed: demo:A@3"), "{error}");
    assert!(key_file.exists(), "the grant may still be held");
    assert!(script.seen[2].contains(&"--live".to_owned()));
}
