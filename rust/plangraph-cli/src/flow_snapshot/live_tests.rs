use super::{read_keeper, READY, SNAPSHOT};
use serde_json::{json, Map};

#[test]
fn ready_failure_keeps_keeper_snapshot() {
    let mut calls = Vec::new();
    let mut readings = Map::new();
    let mut failures = Map::new();
    let snapshot = json!({"items":[{"key":"demo:A","state":"open","epoch":0,"depends_on":[]}],
                          "activity":[]});
    read_keeper(
        |path| {
            calls.push(path.to_owned());
            match path {
                SNAPSHOT => Ok(snapshot.clone()),
                READY => Err("fixture unavailable".to_owned()),
                _ => panic!("unexpected Keeper request {path}"),
            }
        },
        &mut readings,
        &mut failures,
    );
    assert_eq!(calls, [SNAPSHOT, "v1/ready?fmt=json&agent=true&all=1"]);
    assert_eq!(readings["keeper"], snapshot);
    assert!(readings.get("ready").is_none());
    assert_eq!(failures["ready"], "fixture unavailable");
}
