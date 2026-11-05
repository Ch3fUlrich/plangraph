//! Read-only injected acquisition: `acquire` parses bytes; `fold` reduces JSON values.
//! Input envelopes are `{schema_version:1, observed_at, events:[Event], complete:true}`;
//! complete is required only for the separate ask/claim listings. Sources are caller
//! path identifiers, never opened here. Event source and event_id must be nonempty.
//! Output schema 1 includes time, (start_exclusive,end_inclusive], ordered signals,
//! normalized events and pending GOs. Units: tokens, events, asks, IDs/second,
//! claims, seconds. Context is the interval maximum; compactions count events;
//! inbound is distinct IDs / horizon; listings count latest per-ID open/active
//! states at now; GO latency is maximum completed duration by authorization time.
//! Missing context/completions are unknown; explicit empty complete listings count 0.
//! Later Rust hysteresis consumes these values. Private Python scripts read JSON
//! or call a later thin binary, not import Rust through Python SourceFileLoader.
//!
//! Acquisition contract:
//! - Slot zero is a successful, complete observation stream for this interval;
//!   an explicit empty events array certifies zero compactions and inbound IDs.
//! - Slots one and two are complete state listings, including any history needed
//!   to reconstruct the latest state. Their observation times certify completeness.
//! - No source is read, no wall clock is consulted, and no client is accepted.
//! - `observed_at` is acquisition time, not an invented sample time. Event times
//!   and sources remain in `events`, even when their signal has diagnostics.
//! - Unknown reasons describe acquisition/validation failure, not capacity.
//! - Exact event replay is idempotent within each kind. IDs of inbound messages
//!   additionally deduplicate different observations of the same message.
//! - Pending metadata contains submitted GOs without an authorization timestamp;
//!   it is never included in the completed-duration maximum.
//! - Invalid envelopes cannot certify an empty stream. Invalid typed samples
//!   poison only their kind; an unidentifiable kind poisons its input slot.
//! - A wholly unreadable primary envelope makes the entire snapshot unknown.
//!   Independent listing failures leave stream-derived values intact.
//! - Serialization uses serde's externally tagged Known/Unknown readings;
//!   consumers must handle Unknown rather than supplying a numeric default.
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::{BTreeMap, BTreeSet};

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
#[rustfmt::skip]
pub enum Kind {
    Ctx { tokens: u64 },
    Compaction,
    Ask { id: String, open: bool },
    Inbound { id: String },
    Claim { id: String, active: bool },
    Go { id: String, submitted_at: i64, authorized_at: Option<i64> },
}
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Event {
    pub event_id: String,
    pub timestamp: i64,
    pub source: String,
    #[serde(flatten)]
    pub kind: Kind,
}
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[rustfmt::skip]
pub enum Reading { Known(f64), Unknown(String) }
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Signal {
    pub name: String,
    pub value: Reading,
    pub unit: String,
    pub observed_at: Option<i64>,
    pub provenance: Vec<String>,
}
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Snapshot {
    pub schema_version: u8,
    pub timestamp: i64,
    pub start_exclusive: Option<i64>,
    pub end_inclusive: i64,
    pub signals: Vec<Signal>,
    pub events: Vec<Event>,
    pub pending: Vec<Event>,
}
#[rustfmt::skip]
const NAMES: [&str; 6] = ["rolling_ctx", "compactions", "open_asks", "inbound_rate", "claims", "go_latency"];
const UNITS: [&str; 6] = [
    "tokens",
    "events",
    "asks",
    "IDs/second",
    "claims",
    "seconds",
];
#[rustfmt::skip]
fn index(kind: &str) -> Option<usize> {
    ["ctx", "compaction", "ask", "inbound", "claim", "go"].iter().position(|s| *s == kind)
}
/// Inputs in order: event stream, complete ask listing, complete claim listing.
/// Err preserves acquisition failure; Ok(empty bytes) is not an empty listing.
#[rustfmt::skip]
pub fn acquire(inputs: [Result<&[u8], &str>; 3], sources: [&str; 3], now_s: i64, horizon_s: i64) -> Snapshot {
    fold(inputs.map(|r| r.map_err(str::to_owned).and_then(|b| serde_json::from_slice(b).map_err(|e| e.to_string()))), sources, now_s, horizon_s)
}
/// Pure fold; future envelope timestamps also detect a backward injected clock.
#[rustfmt::skip]
pub fn fold(inputs: [Result<Value, String>; 3], sources: [&str; 3], now_s: i64, horizon_s: i64) -> Snapshot {
    let start = now_s.checked_sub(horizon_s).filter(|_| horizon_s > 0);
    let mut out = Snapshot { schema_version: 1, timestamp: now_s, start_exclusive: start, end_inclusive: now_s,
        signals: (0..6).map(|i| Signal { name: NAMES[i].into(), unit: UNITS[i].into(), value: Reading::Unknown("missing samples".into()), observed_at: None, provenance: vec![] }).collect(), events: vec![], pending: vec![] };
    if inputs[0].as_ref().map_or(true, |v| !v.is_object() || !v["events"].is_array()) {
        for s in &mut out.signals {
            s.value = Reading::Unknown("unparseable envelope".into());
            s.provenance = sources.iter().map(|s| (*s).into()).collect();
        }
        return out;
    }
    for (slot, input) in inputs.into_iter().enumerate() {
        let affected: &[usize] = match slot { 0 => &[0,1,3,5], 1 => &[2], _ => &[4] };
        let parsed = input.and_then(|v| {
            let ts = v["observed_at"].as_i64().ok_or("missing listing/sample timestamp")?;
            if v["schema_version"] != 1 || ts > now_s || start.is_none() || sources[slot].trim().is_empty() {
                return Err("invalid schema, provenance or clock".into());
            }
            if slot > 0 && v["complete"] != true { return Err("incomplete listing".into()); }
            let events = v["events"].as_array().ok_or("invalid envelope events")?.clone();
            Ok((ts, events))
        });
        for &i in affected { out.signals[i].provenance.push(sources[slot].into()); }
        let (ts, raw) = match parsed {
            Ok(v) => v,
            Err(e) => { for &i in affected { out.signals[i].value = Reading::Unknown(e.clone()); } continue; }
        };
        let mut errors = BTreeMap::new();
        let mut seen: BTreeMap<(usize, String), Event> = BTreeMap::new();
        for v in raw {
            let target = v["kind"].as_str().and_then(index);
            let targets = target.map(|i| vec![i]).unwrap_or_else(|| affected.to_vec());
            for &i in &targets {
                if affected.contains(&i) {
                    if let Some(s) = v["source"].as_str() { out.signals[i].provenance.push(s.into()); }
                }
            }
            let event = serde_json::from_value::<Event>(v).ok().filter(|e| {
                !e.event_id.trim().is_empty() && !e.source.trim().is_empty() && e.timestamp <= ts && match &e.kind {
                    Kind::Go { id, submitted_at, authorized_at } => !id.is_empty() && *submitted_at >= 0 && *submitted_at <= e.timestamp && authorized_at.is_none_or(|a| a >= *submitted_at && a <= e.timestamp),
                    Kind::Ask { id, .. } | Kind::Claim { id, .. } | Kind::Inbound { id } => !id.is_empty(),
                    _ => true,
                }
            });
            let Some(e) = event else {
                for i in targets { if affected.contains(&i) { errors.insert(i, "invalid sample"); } }
                continue;
            };
            let i = target.expect("typed kind has index");
            if !affected.contains(&i) { continue; }
            let key = (i, e.event_id.clone());
            if let Some(old) = seen.get(&key) {
                if old != &e { errors.insert(i, "conflicting event ID"); }
            } else { seen.insert(key, e); }
        }
        for &i in affected {
            let events: Vec<_> = seen.iter().filter(|((k,_),_)| *k == i).map(|(_,e)| e).collect();
            let in_window = |t| start.is_some_and(|s| t > s && t <= now_s);
            let mut values = vec![];
            let mut ids = BTreeSet::new();
            let mut states = BTreeMap::new();
            for e in &events {
                match &e.kind {
                    Kind::Ctx { tokens } if in_window(e.timestamp) => values.push(*tokens as f64),
                    Kind::Compaction if in_window(e.timestamp) => values.push(1.0),
                    Kind::Inbound { id } if in_window(e.timestamp) => { ids.insert(id); }
                    Kind::Go { authorized_at: Some(a), submitted_at, .. } if in_window(*a) => values.push((*a - *submitted_at) as f64),
                    Kind::Go { authorized_at: None, .. } => out.pending.push((*e).clone()),
                    Kind::Ask { id, open } | Kind::Claim { id, active: open } => {
                        let old = states.entry(id).or_insert((e.timestamp, *open));
                        if e.timestamp == old.0 && *open != old.1 { errors.insert(i, "conflicting latest state"); }
                        if e.timestamp > old.0 { *old = (e.timestamp, *open); }
                    }
                    _ => (),
                }
            }
            let value = match i {
                0 | 5 => values.into_iter().reduce(f64::max).map(Reading::Known).unwrap_or(Reading::Unknown("missing samples".into())),
                1 => Reading::Known(values.len() as f64),
                3 => Reading::Known(ids.len() as f64 / horizon_s as f64),
                _ => Reading::Known(states.values().filter(|(_, active)| *active).count() as f64),
            };
            let s = &mut out.signals[i];
            s.value = errors.get(&i).map(|e| Reading::Unknown((*e).into())).unwrap_or(value);
            s.observed_at = Some(ts);
            s.provenance.sort(); s.provenance.dedup();
        }
        out.events.extend(seen.into_values());
    }
    out
}

#[cfg(test)]
#[rustfmt::skip]
mod tests {
    use super::*;
    use serde_json::json;
    fn event(kind: &str, id: &str, t: i64, extra: Value) -> Value {
        let mut v = json!({"kind":kind,"event_id":id,"timestamp":t,"source":"sample"});
        v.as_object_mut().unwrap().extend(extra.as_object().unwrap().clone()); v
    }
    fn envelope(events: Vec<Value>) -> Value {
        json!({"schema_version":1,"observed_at":100,"complete":true,"events":events})
    }
    fn run(events: Vec<Value>, asks: Vec<Value>, claims: Vec<Value>) -> Snapshot {
        fold([Ok(envelope(events)),Ok(envelope(asks)),Ok(envelope(claims))], ["events","asks","claims"],100,10)
    }
    fn unknown(s: &Snapshot, i: usize) { assert!(matches!(&s.signals[i].value, Reading::Unknown(r) if !r.is_empty())); }
    #[test]
    fn six_ordered_roundtrip() {
        let s = run(vec![event("ctx","c",99,json!({"tokens":42})),event("compaction","x",100,json!({})),
            event("inbound","i",99,json!({"id":"message"})),event("go","g",100,json!({"id":"g","submitted_at":94,"authorized_at":99}))],
            vec![event("ask","a",98,json!({"id":"a","open":true}))],vec![event("claim","l",97,json!({"id":"l","active":true}))]);
        assert_eq!(s.signals.len(),6);
        for (i, value) in [42.,1.,1.,0.1,1.,5.].into_iter().enumerate() {
            assert_eq!(s.signals[i].name,NAMES[i]); assert_eq!(s.signals[i].unit,UNITS[i]);
            assert_eq!(s.signals[i].value,Reading::Known(value)); assert_eq!(s.signals[i].observed_at,Some(100));
            assert!(s.signals[i].provenance.contains(&"sample".into()));
        }
        assert_eq!((s.schema_version,s.timestamp,s.start_exclusive,s.end_inclusive),(1,100,Some(90),100));
        assert_eq!(serde_json::from_slice::<Snapshot>(&serde_json::to_vec(&s).unwrap()).unwrap(),s);
    }
    #[test]
    fn boundaries_replay_latest_pending() {
        let c = event("compaction","x",100,json!({}));
        let s = run(vec![c.clone(),c,event("compaction","left",90,json!({})),
            event("ctx","old",90,json!({"tokens":999})),event("ctx","c",99,json!({"tokens":12})),event("ctx","d",100,json!({"tokens":8})),
            event("go","pending",99,json!({"id":"p","submitted_at":95,"authorized_at":null})),
            event("go","done",100,json!({"id":"d","submitted_at":90,"authorized_at":100}))],
            vec![event("ask","a2",100,json!({"id":"a","open":false})),event("ask","a1",80,json!({"id":"a","open":true}))],
            vec![event("claim","b1",80,json!({"id":"b","active":true})),event("claim","b2",100,json!({"id":"b","active":false}))]);
        for (i,v) in [(0,12.),(1,1.),(2,0.),(4,0.),(5,10.)] { assert_eq!(s.signals[i].value,Reading::Known(v)); }
        assert_eq!(s.pending.len(),1); assert_eq!(s.events.len(),11);
        let empty = run(vec![],vec![],vec![]);
        assert_eq!(empty.signals[2].value,Reading::Known(0.)); assert_eq!(empty.signals[4].value,Reading::Known(0.));
    }
    #[test]
    fn missing_failed_scoped() {
        let s = fold([Ok(envelope(vec![])),Err("listing failed".into()),Ok(envelope(vec![]))],["e","a","c"],100,10);
        for i in [0,2,5] { unknown(&s,i); }
        assert_eq!(s.signals[2].value,Reading::Unknown("listing failed".into()));
        for i in [1,3,4] { assert_eq!(s.signals[i].value,Reading::Known(0.)); }
    }
    #[test]
    fn invalid_scoped_and_clock() {
        for bad in [event("ctx","c",99,json!({"tokens":-1})),event("ctx","c",101,json!({"tokens":2})),
            event("ctx","c",99,json!({"tokens":"bad"})),json!({"kind":"ctx","event_id":"c","timestamp":99,"tokens":2})] {
            let s = run(vec![bad],vec![],vec![]); unknown(&s,0); assert_eq!(s.signals[1].value,Reading::Known(0.));
        }
        let s = run(vec![event("ctx","c",99,json!({"tokens":1})),event("ctx","c",99,json!({"tokens":2})),
            event("go","g",99,json!({"id":"g","submitted_at":98,"authorized_at":97}))],vec![],vec![]);
        unknown(&s,0); unknown(&s,5);
        for now in [99,100] {
            let inputs = if now == 99 { [Ok(envelope(vec![])),Ok(envelope(vec![])),Ok(envelope(vec![]))] } else { [Ok(json!(null)),Ok(json!(null)),Ok(json!(null))] };
            let s = fold(inputs,["e","a","c"],now,10); for i in 0..6 { unknown(&s,i); }
        }
        let s = acquire([Ok(b"{"),Ok(b"{"),Ok(b"{")],["e","a","c"],100,10);
        for i in 0..6 { unknown(&s,i); }
    }
    #[test]
    fn source_bytes_unchanged() {
        let bytes = serde_json::to_vec(&envelope(vec![])).unwrap(); let before = bytes.clone();
        let s = acquire([Ok(&bytes),Ok(&bytes),Ok(&bytes)],["e","a","c"],100,10);
        assert_eq!(bytes,before); assert_eq!(s.signals[1].value,Reading::Known(0.));
    }
    #[test]
    fn distinct_inbound_and_listing_failures() {
        let s = run(vec![event("inbound","one",91,json!({"id":"m"})),
            event("inbound","two",99,json!({"id":"m"})),
            event("inbound","three",100,json!({"id":"n"})),
            event("inbound","left",90,json!({"id":"excluded"}))],vec![],vec![]);
        assert_eq!(s.signals[3].value,Reading::Known(0.2));
        for listing in [Err("missing listing".into()),Ok(json!({"schema_version":1,"events":[]})),
            Ok(json!({"schema_version":1,"observed_at":100,"events":[],"complete":false}))] {
            let s = fold([Ok(envelope(vec![])),Ok(envelope(vec![])),listing],["e","a","c"],100,10);
            unknown(&s,4); assert_eq!(s.signals[2].value,Reading::Known(0.));
        }
    }
    #[test]
    fn invalid_go_and_provenance_diagnostics() {
        for extra in [json!({"id":"g","submitted_at":-1,"authorized_at":99}),
            json!({"id":"g","submitted_at":98,"authorized_at":101}),
            json!({"id":"g","submitted_at":99,"authorized_at":98})] {
            let s = run(vec![event("go","g",100,extra)],vec![],vec![]);
            unknown(&s,5); assert_eq!(s.signals[1].value,Reading::Known(0.));
            assert!(s.signals[5].provenance.contains(&"sample".into()));
        }
        let s = fold([Ok(envelope(vec![])),Ok(envelope(vec![])),Ok(envelope(vec![]))],["e","","c"],100,10);
        unknown(&s,2); assert_eq!(s.signals[4].value,Reading::Known(0.));
        for horizon in [0,-1,i64::MAX] {
            let s = fold([Ok(envelope(vec![])),Ok(envelope(vec![])),Ok(envelope(vec![]))],["e","a","c"],i64::MIN,horizon);
            for i in 0..6 { unknown(&s,i); }
        }
    }
    #[test]
    fn unknown_kind_and_conflicting_listing() {
        let s = run(vec![json!({"kind":"invalid","source":"diagnostic"})],vec![],vec![]);
        for i in [0,1,3,5] { unknown(&s,i); assert!(s.signals[i].provenance.contains(&"diagnostic".into())); }
        assert_eq!(s.signals[2].value,Reading::Known(0.));
        let s = run(vec![],vec![event("ask","same",99,json!({"id":"a","open":true})),
            event("ask","same",99,json!({"id":"a","open":false}))],vec![]);
        unknown(&s,2); assert_eq!(s.signals[4].value,Reading::Known(0.));
        let s = fold([Ok(json!(null)),Ok(envelope(vec![])),Ok(envelope(vec![]))],["e","a","c"],100,10);
        for i in 0..6 { unknown(&s,i); assert_eq!(s.signals[i].provenance,vec!["e","a","c"]); }
    }
}
