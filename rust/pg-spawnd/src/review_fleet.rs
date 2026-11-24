//! Consume host lifecycle pages using one durable bookmark per host.
use crate::{adapter::Adapter, health_report_rows::regular};
use pg_worker_core::{lockfile::try_lock, run::record_io::atomic_write_private};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::{
    collections::{BTreeMap, BTreeSet},
    fs,
    path::Path,
    time::{Duration, Instant},
};

#[derive(Default, Deserialize, Serialize)]
struct Cache {
    cursor: Value,
    hosts: BTreeMap<String, BTreeMap<String, Value>>,
}

fn request(state: &Path, args: &[String], timeout: Duration) -> Result<Value, String> {
    let (code, output, _) = Adapter(state)
        .run(args, timeout)
        .map_err(|e| e.into_error_data().message.to_string())?;
    if code != 0 {
        return Err("review fleet adapter failed".into());
    }
    serde_json::from_slice(&output).map_err(|_| "invalid review fleet adapter output".into())
}

fn page(cache: &mut Cache, result: &Value) -> Result<(BTreeSet<String>, bool), String> {
    let hosts = result["hosts"]
        .as_object()
        .ok_or("invalid lifecycle hosts")?;
    let mut failed = BTreeSet::new();
    let mut more = false;
    cache
        .hosts
        .retain(|host, _| host == "@local" || hosts.contains_key(host));
    let mut cursor = result["next_cursor"].clone();
    for (host, data) in std::iter::once(("@local", &result["local"]))
        .chain(hosts.iter().map(|(host, data)| (host.as_str(), data)))
    {
        if data.get("error").is_some() {
            failed.insert(host.to_owned());
            continue;
        }
        let rows = data["rows"].as_array().ok_or("invalid lifecycle rows")?;
        if data["review_projection"] != 1
            || rows.iter().any(|row| {
                row["role"] == "review"
                    && row["origin_receipt"] != true
                    && !row["review_activity"].is_boolean()
            })
        {
            failed.insert(host.to_owned());
            // Re-read this page after the host upgrades its projection.
            if host == "@local" {
                cursor["local"] = json!(cache.cursor["local"].as_u64().unwrap_or(0));
            } else {
                cursor["hosts"][host] = json!(cache.cursor["hosts"][host].as_u64().unwrap_or(0));
            }
            continue;
        }
        more |= data["more"].as_bool().ok_or("invalid lifecycle page")?;
        let runs = cache.hosts.entry(host.to_owned()).or_default();
        for row in rows {
            if row["origin_receipt"] == true
                || row["role"] != "review" && row["role"] != "implement"
            {
                continue;
            }
            let id = row["run_id"].as_str().ok_or("missing lifecycle run id")?;
            if row["role"] == "review" || row["native_review_rounds"].is_array() {
                runs.insert(id.to_owned(), row.clone());
            }
        }
    }
    cache.cursor = cursor;
    Ok((failed, more))
}

fn lifecycle(state: &Path) -> Result<(Cache, BTreeSet<String>, bool), String> {
    let dir = state.join("reports");
    let path = dir.join("review-lifecycle.json");
    regular(state, true)
        .and_then(|_| regular(&dir, true))
        .and_then(|_| regular(&path, false))
        .map_err(|e| e.to_string())?;
    fs::create_dir_all(&dir).map_err(|e| e.to_string())?;
    let _lock = try_lock(&dir.join("review-lifecycle.lock"))
        .map_err(|e| e.to_string())?
        .ok_or("review lifecycle collection already running")?;
    let mut cache = match fs::read(&path) {
        Ok(bytes) => {
            serde_json::from_slice::<Cache>(&bytes).map_err(|_| "invalid review lifecycle cache")?
        }
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Cache::default(),
        Err(_) => return Err("review lifecycle cache unavailable".into()),
    };
    let deadline = Instant::now() + Duration::from_secs(15);
    loop {
        let cursor = if cache.cursor.is_null() {
            "{}".into()
        } else {
            cache.cursor.to_string()
        };
        let result = request(
            state,
            &["runs".into(), "--changed-since".into(), cursor],
            deadline.saturating_duration_since(Instant::now()),
        );
        let (failed, more) = match result.and_then(|result| page(&mut cache, &result)) {
            Ok(summary) => summary,
            Err(_) => return Ok((cache, BTreeSet::from(["lifecycle adapter".into()]), true)),
        };
        atomic_write_private(
            &path,
            &serde_json::to_vec(&cache).map_err(|e| e.to_string())?,
        )
        .map_err(|e| e.to_string())?;
        if !more || Instant::now() + Duration::from_secs(5) >= deadline {
            return Ok((cache, failed, more));
        }
    }
}

pub(crate) fn collect(state: &Path, since: i64, until: i64) -> Value {
    let (cache, mut failed, more) = lifecycle(state).unwrap_or_else(|_| {
        (
            Cache::default(),
            BTreeSet::from(["lifecycle collection".into()]),
            true,
        )
    });
    let mut rows = Vec::new();
    for (host, runs) in &cache.hosts {
        for row in runs.values() {
            if row["role"] == "review" && !row["review_activity"].is_boolean() {
                failed.insert(host.clone());
            }
            if row["ended"].as_f64().is_none_or(|end| end >= since as f64)
                || row["native_review_rounds"].is_array()
            {
                rows.push(row);
            }
        }
    }
    let receipts = request(
        state,
        &[
            "review-receipts".into(),
            since.to_string(),
            until.to_string(),
        ],
        Duration::from_secs(12),
    )
    .unwrap_or(Value::Null);
    if receipts["complete"] != true {
        failed.insert("keeper receipts".into());
    }
    json!({
        "runs":rows, "keeper_seats":receipts["keeper_seats"],
        "head_acceptances":receipts["head_acceptances"],
        "coverage":{"scope":"fleet", "complete":failed.is_empty() && !more,
                    "incomplete_sources":failed, "pages_pending":more}
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn leaves_unsupported_hosts_at_their_bookmark_until_projection_upgrades() {
        let mut cache = Cache::default();
        let mut incoming = json!({
            "local":{"rows":[],"next_cursor":0,"more":false,"review_projection":1},
            "hosts":{
                "older":{"rows":[],"next_cursor":9,"more":false},
                "newer":{"rows":[{"run_id":"run-1","role":"review","review_activity":true}],
                         "next_cursor":1,"more":true,"review_projection":1}
            },
            "next_cursor":{"local":0,"hosts":{"older":9,"newer":1}}
        });
        let (failed, more) = page(&mut cache, &incoming).unwrap();
        assert_eq!(failed, BTreeSet::from(["older".to_owned()]));
        assert!(more);
        assert_eq!(cache.cursor["hosts"], json!({"older":0,"newer":1}));
        assert!(cache.hosts["newer"]["run-1"]["review_activity"] == true);
        incoming["hosts"]["older"]["review_projection"] = json!(1);
        assert!(page(&mut cache, &incoming).unwrap().0.is_empty());
        assert_eq!(cache.cursor["hosts"]["older"], 9);
    }
}
