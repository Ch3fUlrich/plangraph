use pg_ctx::mirror::{Alarm, Mirror};
use std::fs;
#[test]
fn every_configured_repository_is_attempted_after_failure() {
    let tmp = tempfile::tempdir().unwrap();
    let config = tmp.path().join("repos.json");
    fs::write(
        &config,
        serde_json::to_vec(&serde_json::json!({
            "first": tmp.path().join("absent-first"),
            "second": tmp.path().join("absent-second")
        }))
        .unwrap(),
    )
    .unwrap();
    let mirror = Mirror::from_repos(&config, &tmp.path().join("mirrors")).unwrap();
    let result = mirror.fetch_all();
    assert_eq!(result.len(), 2);
    assert_eq!(result["first"], Err(Alarm::FetchFailed));
    assert_eq!(result["second"], Err(Alarm::FetchFailed));
    assert!(tmp.path().join("mirrors/second").is_dir());
}
