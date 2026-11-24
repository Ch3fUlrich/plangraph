use super::*;
use crate::lane_roster::LaneKind;

fn lane(name: &str, kind: LaneKind, keys: &[&str]) -> Lane {
    Lane {
        name: name.into(),
        kind,
        detail: "d1".into(),
        keys: keys.iter().map(|k| (*k).to_owned()).collect(),
    }
}

fn keys(list: &[&str]) -> BTreeSet<String> {
    list.iter().map(|k| (*k).to_owned()).collect()
}

#[test]
fn a_held_item_is_refused_naming_the_lane() {
    let lanes = [lane("batch1", LaneKind::Native, &["p:A"])];
    let err = admit(&keys(&["p:A", "p:B"]), &lanes, Some("batch2"), None).unwrap_err();
    let text = err.to_string();
    assert!(
        text.contains("p:A is held by native run batch1 (d1)"),
        "{text}"
    );
    assert!(!text.contains("p:B is held"), "{text}");
}

#[test]
fn the_lane_itself_and_free_items_are_admitted() {
    let lanes = [lane("batch1", LaneKind::Claude, &["p:A"])];
    assert_eq!(
        admit(&keys(&["p:A"]), &lanes, Some("batch1"), None),
        Ok(Admission::default())
    );
    assert_eq!(
        admit(&keys(&["p:Z"]), &lanes, None, None),
        Ok(Admission::default())
    );
}

#[test]
fn an_override_without_a_reason_is_refused_even_without_a_conflict() {
    let lanes = [lane("batch1", LaneKind::Native, &["p:A"])];
    for blank in ["", "  "] {
        assert_eq!(
            admit(&keys(&["p:A"]), &lanes, None, Some(blank)),
            Err(GuardError::OverrideNeedsReason)
        );
        assert_eq!(
            admit(&keys(&["p:Z"]), &lanes, None, Some(blank)),
            Err(GuardError::OverrideNeedsReason)
        );
    }
}

#[test]
fn an_authorized_override_admits_the_second_lane_and_records_its_reason() {
    let lanes = [lane("batch1", LaneKind::Native, &["p:A"])];
    let admission = admit(
        &keys(&["p:A"]),
        &lanes,
        None,
        Some(" retry of a dead lane "),
    )
    .unwrap();
    assert_eq!(admission.reason.as_deref(), Some("retry of a dead lane"));
    assert_eq!(admission.overridden[0].key, "p:A");
    let dir = tempfile::tempdir().unwrap();
    let log = dir.path().join("sub/overrides.jsonl");
    record_override(&log, "native-submit", &admission, 7).unwrap();
    let line: Value = serde_json::from_str(std::fs::read_to_string(&log).unwrap().trim()).unwrap();
    assert_eq!(line["reason"], "retry of a dead lane");
    assert_eq!(line["held"][0]["lane"], "native run batch1 (d1)");
    assert_eq!(line["context"], "native-submit");
}

fn claim(key: &str, state: &str, by: &str, merged: bool) -> Claim {
    Claim {
        key: key.into(),
        epoch: 3,
        state: state.into(),
        claimed_by: by.into(),
        merged,
    }
}

#[test]
fn only_held_items_of_live_lanes_are_renewed() {
    let lanes = [
        lane("n1", LaneKind::Native, &["p:NATIVE"]),
        lane("c1", LaneKind::Claude, &["p:CLAUDE"]),
        lane("pr", LaneKind::PullRequest, &["p:PR"]),
    ];
    let claims = [
        claim("p:NATIVE", "claimed", "po", false),
        claim("p:CLAUDE", "claimed", "po", false),
        claim("p:PR", "claimed", "po", false),
        claim("p:DEAD", "claimed", "po", false),
        claim("p:OTHER", "claimed", "someone", false),
        claim("p:MERGED", "claimed", "po", true),
        claim("p:OPEN", "open", "po", false),
    ];
    let plan = heartbeat_plan(&claims, "po", &lanes[..]);
    let renewed: Vec<_> = plan.renew.iter().map(|(k, _)| k.as_str()).collect();
    assert_eq!(renewed, ["p:NATIVE", "p:CLAUDE", "p:PR"]);
    assert_eq!(
        plan.skipped,
        [
            ("p:DEAD".to_owned(), "no_live_lane"),
            ("p:OTHER".to_owned(), "other_holder"),
            ("p:MERGED".to_owned(), "merged"),
            ("p:OPEN".to_owned(), "not_claimed"),
        ]
    );
}
