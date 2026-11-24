//! Merge per-shard pytest duration maps (canonical test id -> seconds) into one input for the next train.
use serde_json::{Map, Value};

pub fn parse(text: &str) -> Result<Map<String, Value>, String> {
    let value: Value = serde_json::from_str(text).map_err(|e| e.to_string())?;
    let map = value
        .as_object()
        .ok_or("expected a JSON object of test id to seconds")?;
    for (id, seconds) in map {
        match seconds.as_f64() {
            Some(s) if s.is_finite() && s >= 0.0 => {}
            _ => return Err(format!("{id}: expected nonnegative seconds")),
        }
    }
    Ok(map.clone())
}

/// The union of the maps; a test seen in two shards keeps the larger time (a retry must not shrink it).
pub fn combine(inputs: &[Map<String, Value>]) -> Map<String, Value> {
    let mut merged: std::collections::BTreeMap<String, f64> = Default::default();
    for map in inputs {
        for (id, seconds) in map {
            let seconds = seconds.as_f64().unwrap_or(0.0);
            let entry = merged.entry(id.clone()).or_insert(seconds);
            if seconds > *entry {
                *entry = seconds;
            }
        }
    }
    merged
        .into_iter()
        .map(|(id, s)| (id, Value::from((s * 1000.0).round() / 1000.0)))
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn shards_merge_into_one_canonical_map_and_a_repeat_keeps_the_larger_time() {
        let a = parse(r#"{"t.py::a": 1.5, "t.py::b": 2}"#).unwrap();
        let b = parse(r#"{"t.py::b": 3.25, "t.py::c": 0}"#).unwrap();
        let merged = combine(&[a, b]);
        assert_eq!(
            serde_json::to_string(&merged).unwrap(),
            r#"{"t.py::a":1.5,"t.py::b":3.25,"t.py::c":0.0}"#
        );
    }

    #[test]
    fn a_negative_or_non_numeric_time_is_refused() {
        assert!(parse(r#"{"t.py::a": -1}"#).is_err());
        assert!(parse(r#"{"t.py::a": "1"}"#).is_err());
        assert!(parse("[1]").is_err());
    }
}
