//! Read-only keeper planning reports.
use chrono::{DateTime, SecondsFormat, Utc};
use postgres::{Client, IsolationLevel, NoTls};
use serde_json::{json, Value};
use std::collections::{BTreeMap, BTreeSet};

struct Item {
    project: String,
    kind: Option<String>,
    state: String,
    operator: bool,
    priority: i64,
    estimate: f64,
}

impl Item {
    fn terminal(&self) -> bool {
        matches!(self.state.as_str(), "done" | "cancelled")
    }

    fn agent(&self) -> bool {
        !self.operator
            && !matches!(
                self.kind.as_deref(),
                Some("question" | "decision" | "operator_task")
            )
    }

    fn open_lane(&self) -> bool {
        self.state == "open" && self.kind.as_deref() == Some("lane")
    }
}

type Items = BTreeMap<String, Item>;
type Edges = BTreeMap<String, BTreeSet<String>>;

fn query_result(value: Value, n: usize, unmatched: usize, unknown: usize) -> Value {
    json!({"value": value, "n": n, "unmatched": unmatched, "unknown": unknown, "gap": unknown > 0 || unmatched > 0})
}

fn keeper_flow(items: &Items, parents: &Edges, children: &Edges, unmatched: usize) -> Value {
    let mut state_counts = BTreeMap::new();
    let mut ready = vec![];
    let mut lower = vec![];
    for (key, item) in items {
        let counts = state_counts.entry(&item.project).or_insert_with(|| {
            BTreeMap::from([
                ("open", 0),
                ("claimed", 0),
                ("in_review", 0),
                ("done", 0),
                ("cancelled", 0),
            ])
        });
        *counts.entry(item.state.as_str()).or_default() += 1;
        let dependencies = parents.get(key).into_iter().flatten();
        if item.state == "open"
            && dependencies
                .clone()
                .all(|dep| items.get(dep).is_some_and(|it| it.state == "done"))
        {
            ready.push(key);
        }
        if item.open_lane() {
            lower.extend(dependencies.filter_map(|dep| {
                let prerequisite = items.get(dep)?;
                (prerequisite.open_lane() && prerequisite.priority < item.priority)
                    .then_some([key, dep])
            }));
        }
    }
    ready.sort_by(|a, b| {
        items[*b]
            .priority
            .cmp(&items[*a].priority)
            .then_with(|| {
                children
                    .get(*b)
                    .map_or(0, BTreeSet::len)
                    .cmp(&children.get(*a).map_or(0, BTreeSet::len))
            })
            .then_with(|| items[*a].estimate.total_cmp(&items[*b].estimate))
            .then_with(|| a.cmp(b))
    });
    let agent: Vec<_> = ready
        .iter()
        .filter(|k| items[**k].agent())
        .copied()
        .collect();
    let lanes: Vec<_> = agent
        .iter()
        .copied()
        .filter(|k| items[*k].open_lane())
        .collect();
    let mut ties: Vec<_> = lanes
        .iter()
        .take_while(|k| items[**k].priority == items[lanes[0]].priority)
        .copied()
        .collect();
    if ties.len() < 2 {
        ties.clear();
    }
    query_result(
        json!({
            "ready": ready, "ready_count": ready.len(),
            "ready_agent": agent, "ready_agent_count": agent.len(),
            "state_counts": state_counts, "top_priority_ties": ties,
            "lower_priority_dependencies": lower
        }),
        items.len(),
        unmatched,
        0,
    )
}

fn critical_fanout(
    items: &Items,
    children: &Edges,
    activity: &BTreeMap<String, Option<DateTime<Utc>>>,
    as_of: DateTime<Utc>,
    unmatched: usize,
) -> Value {
    let mut rows = vec![];
    for (key, item) in items {
        if item.state != "open" || !children.contains_key(key) {
            continue;
        }
        let mut seen = BTreeSet::from([key]);
        let mut todo = vec![key];
        while let Some(node) = todo.pop() {
            for child in children.get(node).into_iter().flatten() {
                if seen.insert(child) {
                    todo.push(child);
                }
            }
        }
        let reach = seen.len() - 1;
        if reach > 0 {
            let idle = activity
                .get(key)
                .copied()
                .flatten()
                .filter(|time| *time <= as_of)
                .map(|time| (as_of - time).num_seconds());
            rows.push((key, reach, idle));
        }
    }
    rows.sort_by(|a, b| {
        b.1.cmp(&a.1)
            .then_with(|| b.2.cmp(&a.2))
            .then_with(|| a.0.cmp(b.0))
    });
    let values: Vec<_> = rows
        .iter()
        .map(|(key, reach, idle)| json!({"key":key, "reach":reach, "idle_age_s":idle}))
        .collect();
    query_result(
        json!(values),
        rows.len(),
        unmatched,
        rows.iter().filter(|row| row.2.is_none()).count(),
    )
}

fn timestamp(value: &str) -> Option<DateTime<Utc>> {
    DateTime::parse_from_rfc3339(value)
        .ok()
        .map(|time| time.with_timezone(&Utc))
}

fn quote_schema(schema: &str) -> Result<String, String> {
    if schema.is_empty()
        || schema.len() > 63
        || !schema.as_bytes()[0].is_ascii_lowercase()
        || !schema
            .bytes()
            .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'_')
    {
        return Err("invalid keeper schema name".into());
    }
    Ok(format!("\"{schema}\""))
}

/// Connect using the reader's local PostgreSQL DSN.
pub fn report(dsn: &str, schema: &str, since: Option<&str>) -> Result<Value, String> {
    let mut client =
        Client::connect(dsn, NoTls).map_err(|_| "cannot connect to keeper PostgreSQL")?;
    report_client(&mut client, schema, since)
}

/// Load both reports within one read-only snapshot.
pub fn report_client(
    client: &mut Client,
    schema: &str,
    since: Option<&str>,
) -> Result<Value, String> {
    let schema = quote_schema(schema)?;
    let since = since
        .map(|value| timestamp(value).ok_or("invalid --since timestamp"))
        .transpose()?;
    let mut tx = client
        .build_transaction()
        .isolation_level(IsolationLevel::RepeatableRead)
        .read_only(true)
        .start()
        .map_err(|_| "cannot begin read-only keeper transaction")?;
    let clock: String = tx.query_one("SELECT to_char(transaction_timestamp() AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"')", &[])
        .map_err(|_| "cannot read keeper transaction time")?.get(0);
    let as_of = timestamp(&clock).ok_or("invalid keeper transaction time")?;
    let items: Items = tx.query(&format!("SELECT key, project, kind, state, operator, priority, estimate_h FROM {schema}.items"), &[])
        .map_err(|_| "cannot read keeper items")?.into_iter().map(|row| {
            (row.get(0), Item {
                project: row.get(1), kind: row.get(2), state: row.get(3),
                operator: row.get::<_, i64>(4) != 0, priority: row.get(5),
                estimate: row.get::<_, Option<f64>>(6).filter(|v| v.is_finite()).unwrap_or(0.0),
            })
        }).collect();
    let mut parents = Edges::new();
    let mut children = Edges::new();
    let mut unmatched = 0;
    for row in tx
        .query(&format!("SELECT src, dst FROM {schema}.edges"), &[])
        .map_err(|_| "cannot read keeper edges")?
    {
        let source: String = row.get(0);
        let target: String = row.get(1);
        let item = items.get(&source);
        let target_exists = items.contains_key(&target);
        if item.is_none() || !target_exists {
            unmatched += 1;
        }
        if target_exists && item.is_some_and(|it| !it.terminal()) {
            children
                .entry(target.clone())
                .or_default()
                .insert(source.clone());
        }
        parents.entry(source).or_default().insert(target);
    }
    let mut activity = BTreeMap::new();
    let mut counts = BTreeMap::new();
    let mut bad_events = 0;
    for row in tx
        .query(
            &format!("SELECT key, ts, type FROM {schema}.events ORDER BY seq"),
            &[],
        )
        .map_err(|_| "cannot read keeper activity")?
    {
        let key: Option<String> = row.get(0);
        let kind: String = row.get(2);
        let value: String = row.get(1);
        let time = timestamp(&value);
        if kind != "heartbeat" {
            if let Some(key) = key {
                activity.insert(key, time);
            }
        }
        match time {
            Some(time) if time <= as_of && since.is_none_or(|since| time >= since) => {
                *counts.entry(kind).or_insert(0) += 1
            }
            None => bad_events += 1,
            _ => {}
        }
    }
    tx.commit().map_err(|_| "cannot finish keeper snapshot")?;
    Ok(json!({
        "as_of": as_of.to_rfc3339_opts(SecondsFormat::Micros, true),
        "q_keeper_flow": keeper_flow(&items, &parents, &children, unmatched),
        "q_critical_fanout": critical_fanout(&items, &children, &activity, as_of, unmatched),
        "event_counts": query_result(json!(counts), counts.values().sum(), 0, bad_events),
    }))
}

#[cfg(test)]
mod tests {
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
}
