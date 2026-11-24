//! Unit tests of the flow and fanout reports over in-memory graphs.
use super::*;

fn item(project: &str, kind: &str, state: &str, priority: i64, estimate: f64) -> Item {
    Item {
        project: project.into(),
        kind: Some(kind.into()),
        state: state.into(),
        operator: false,
        priority,
        estimate,
    }
}

/// Mirrors the Postgres fixture: ties, a lower-priority dependency, a cancelled
/// dependency, a cross-project edge and a two-item cycle.
fn graph() -> (Items, Edges, Edges, usize) {
    let items: Items = [
        ("a:HIGH", item("a", "lane", "open", 9, 3.0)),
        ("a:TIE", item("a", "lane", "open", 9, 1.0)),
        ("a:MANUAL", item("a", "operator_task", "open", 10, 0.0)),
        ("b:LOW", item("b", "lane", "open", 2, 1.0)),
        ("a:MIDDLE", item("a", "lane", "open", 8, 1.0)),
        ("a:TAIL", item("a", "lane", "open", 3, 1.0)),
        ("a:DEAD", item("a", "lane", "cancelled", 10, 1.0)),
        ("b:BLOCKED", item("b", "lane", "open", 8, 1.0)),
        ("a:OLD", item("a", "lane", "done", 1, 1.0)),
        ("a:CYCLE1", item("a", "lane", "open", 1, 1.0)),
        ("b:CYCLE2", item("b", "lane", "open", 1, 1.0)),
    ]
    .into_iter()
    .map(|(key, item)| (key.to_string(), item))
    .collect();
    let edges = [
        ("a:MIDDLE", "b:LOW"),
        ("a:TAIL", "a:MIDDLE"),
        ("a:TAIL", "b:LOW"),
        ("a:HIGH", "a:OLD"),
        ("b:BLOCKED", "a:DEAD"),
        ("a:CYCLE1", "b:CYCLE2"),
        ("b:CYCLE2", "a:CYCLE1"),
        ("a:TAIL", "x:MISSING"),
    ];
    let (mut parents, mut children, mut unmatched) = (Edges::new(), Edges::new(), 0);
    for (source, target) in edges {
        let known = items.contains_key(target);
        unmatched += usize::from(!known);
        if known && !items[source].terminal() {
            children
                .entry(target.into())
                .or_default()
                .insert(source.into());
        }
        parents
            .entry(source.into())
            .or_default()
            .insert(target.into());
    }
    (items, parents, children, unmatched)
}

#[test]
fn flow_orders_both_ready_modes_and_reports_ties_and_lower_priority_dependencies() {
    let (items, parents, children, unmatched) = graph();
    let flow = keeper_flow(&items, &parents, &children, unmatched);
    let value = &flow["value"];
    // A dependency on a missing item is not satisfied: a:TAIL is not ready.
    assert_eq!(
        value["ready"],
        json!(["a:MANUAL", "a:TIE", "a:HIGH", "b:LOW"])
    );
    assert_eq!(value["ready_agent"], json!(["a:TIE", "a:HIGH", "b:LOW"]));
    assert_eq!(value["ready_count"], 4);
    assert_eq!(value["ready_agent_count"], 3);
    // Equal priority, equal fanout: the smaller estimate comes first.
    assert_eq!(value["top_priority_ties"], json!(["a:TIE", "a:HIGH"]));
    assert_eq!(
        value["lower_priority_dependencies"],
        json!([["a:MIDDLE", "b:LOW"], ["a:TAIL", "b:LOW"]])
    );
    assert_eq!(value["state_counts"]["a"]["open"], 6);
    assert_eq!(value["state_counts"]["a"]["cancelled"], 1);
    assert_eq!(value["state_counts"]["b"]["open"], 3);
    assert_eq!(flow["unmatched"], 1);
    assert_eq!(flow["gap"], true);
}

#[test]
fn fanout_is_transitive_survives_cycles_and_counts_unknown_idle() {
    let (items, _, children, unmatched) = graph();
    let as_of = timestamp("2026-01-01T00:00:00Z").unwrap();
    let activity = BTreeMap::from([
        ("b:LOW".to_string(), timestamp("2025-12-31T00:00:00Z")),
        ("a:MIDDLE".to_string(), None),
    ]);
    let fanout = critical_fanout(&items, &children, &activity, as_of, unmatched);
    let rows = fanout["value"].as_array().unwrap();
    // b:LOW reaches a:MIDDLE directly, and a:TAIL both directly and through a:MIDDLE.
    assert_eq!(
        rows[0],
        json!({"key": "b:LOW", "reach": 2, "idle_age_s": 86_400})
    );
    // Unknown idle ages sort after known ones; equal rows fall back to the key.
    let rest: Vec<_> = rows[1..]
        .iter()
        .map(|row| (row["key"].as_str().unwrap(), row["reach"].as_u64().unwrap()))
        .collect();
    assert_eq!(rest, [("a:CYCLE1", 1), ("a:MIDDLE", 1), ("b:CYCLE2", 1)]);
    assert!(rows[1..].iter().all(|row| row["idle_age_s"].is_null()));
    // Cancelled and done items never appear; unknown idle ages are counted, not guessed.
    assert_eq!(fanout["n"], 4);
    assert_eq!(fanout["unknown"], 3);
    assert_eq!(fanout["gap"], true);
}

#[test]
fn an_activity_time_after_the_snapshot_is_unknown() {
    let (items, _, children, _) = graph();
    let as_of = timestamp("2026-01-01T00:00:00Z").unwrap();
    let activity = BTreeMap::from([("b:LOW".to_string(), timestamp("2026-02-01T00:00:00Z"))]);
    let fanout = critical_fanout(&items, &children, &activity, as_of, 0);
    assert!(fanout["value"][0]["idle_age_s"].is_null());
}

#[test]
fn schema_names_are_validated_before_quoting() {
    assert_eq!(quote_schema("keeper").unwrap(), "\"keeper\"");
    assert!(quote_schema("Keeper;drop").is_err());
    assert!(quote_schema("").is_err());
}
