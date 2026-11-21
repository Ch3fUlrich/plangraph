use super::{sources, Config, Event, Observation, Result};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;
use std::process::Command;
use std::time::Instant;

pub(super) fn valid_repo(repo: &str) -> bool {
    repo.split_once('/').is_some_and(|(owner, name)| {
        [owner, name].iter().all(|part| {
            !part.is_empty()
                && *part != "."
                && *part != ".."
                && part
                    .bytes()
                    .all(|byte| byte.is_ascii_alphanumeric() || b"._-".contains(&byte))
        })
    })
}

fn api(config: &Config, endpoint: &str, paginate: bool, deadline: Instant) -> Result<Vec<Value>> {
    let mut cmd = Command::new("gh");
    cmd.arg("api");
    if paginate {
        cmd.arg("--paginate");
    }
    cmd.arg(format!("repos/{}/{endpoint}", config.github_repo));
    let raw = sources::command(cmd, deadline)?;
    serde_json::Deserializer::from_str(&raw)
        .into_iter()
        .collect::<std::result::Result<_, _>>()
        .map_err(|_| "invalid GitHub response".into())
}

fn object(config: &Config, endpoint: &str, deadline: Instant) -> Result<Value> {
    api(config, endpoint, false, deadline)?
        .pop()
        .ok_or("empty GitHub response".into())
}

fn list(
    config: &Config,
    endpoint: &str,
    field: Option<&str>,
    deadline: Instant,
) -> Result<Vec<Value>> {
    let mut rows = Vec::new();
    for page in api(config, endpoint, true, deadline)? {
        let page = field
            .map_or(&page, |key| &page[key])
            .as_array()
            .ok_or("missing GitHub page")?;
        rows.extend(page.iter().cloned());
    }
    Ok(rows)
}

fn id(value: &Value, field: &str) -> Result<u64> {
    value[field]
        .as_u64()
        .filter(|n| *n > 0)
        .ok_or_else(|| format!("missing GitHub {field}"))
}

fn save_row(
    seen: &mut BTreeMap<String, String>,
    events: &mut Vec<Event>,
    key: String,
    kind: &str,
    data: Value,
) {
    let digest = format!("{:x}", Sha256::digest(data.to_string().as_bytes()));
    if seen.get(&key) != Some(&digest) {
        seen.insert(key, digest);
        let mut event = Event::new("github", kind, data);
        event.untrusted = kind == "pr_timeline";
        events.push(event);
    }
}

fn observe_attempts(
    run: u64,
    count: u64,
    seen: &mut BTreeMap<String, String>,
    events: &mut Vec<Event>,
    mut read: impl FnMut(u64) -> Result<Value>,
) -> Result<()> {
    for attempt in 1..=count {
        let key = format!("run:{run}:{attempt}");
        // A completed attempt is immutable. GitHub preserves each rerun's own jobs.
        if seen.contains_key(&format!("completed:{key}")) {
            continue;
        }
        let data = read(attempt)?;
        let complete = data["attempt"]["status"] == "completed"
            && data["jobs"].as_array().is_some_and(|jobs| {
                !jobs.is_empty() && jobs.iter().all(|job| job["status"] == "completed")
            });
        save_row(seen, events, key.clone(), "workflow_attempt", data);
        if complete {
            seen.insert(format!("completed:{key}"), String::new());
        }
    }
    Ok(())
}

pub(super) fn history(config: &Config, cursor: Value, deadline: Instant) -> Result<Observation> {
    let mut seen: BTreeMap<String, String> = if cursor.is_null() {
        BTreeMap::new()
    } else {
        serde_json::from_value(cursor).map_err(|_| "invalid GitHub cursor")?
    };
    let mut runs = BTreeMap::new();
    let mut events = Vec::new();
    let cutoff = chrono::DateTime::parse_from_rfc3339(&config.github_since)
        .map_err(|_| "invalid GitHub baseline")?
        .with_timezone(&chrono::Utc)
        .format("%Y-%m-%dT%H%%3A%M%%3A%SZ")
        .to_string();
    let main =
        format!("actions/workflows/tests.yml/runs?branch=main&per_page=100&created=%3E%3D{cutoff}");
    for run in list(config, &main, Some("workflow_runs"), deadline)? {
        if run["head_branch"] == "main" {
            runs.insert(id(&run, "id")?, id(&run, "run_attempt")?);
        }
    }
    for &pr in &config.pull_requests {
        let pull = object(config, &format!("pulls/{pr}"), deadline)?;
        let head = pull["head"]["sha"]
            .as_str()
            .filter(|s| super::local::sha(s))
            .ok_or("missing PR head")?;
        for run in list(
            config,
            &format!("actions/runs?per_page=100&head_sha={head}"),
            Some("workflow_runs"),
            deadline,
        )? {
            runs.insert(id(&run, "id")?, id(&run, "run_attempt")?);
        }
        for row in list(
            config,
            &format!("issues/{pr}/timeline?per_page=100"),
            None,
            deadline,
        )? {
            let identity = row["node_id"]
                .as_str()
                .filter(|id| !id.is_empty())
                .map(str::to_owned)
                .unwrap_or_else(|| format!("{:x}", Sha256::digest(row.to_string().as_bytes())));
            save_row(
                &mut seen,
                &mut events,
                format!("pr:{pr}:{identity}"),
                "pr_timeline",
                json!({"pr":pr,"row":row}),
            );
        }
    }
    // Keep observing known run IDs even after a PR moves to another head.
    let known: Vec<u64> = seen
        .keys()
        .filter_map(|key| key.strip_prefix("known:")?.parse().ok())
        .collect();
    for run in known {
        if let std::collections::btree_map::Entry::Vacant(entry) = runs.entry(run) {
            let live = object(config, &format!("actions/runs/{run}"), deadline)?;
            entry.insert(id(&live, "run_attempt")?);
        }
    }
    let mut failed = false;
    for (run, count) in runs.into_iter().rev() {
        seen.insert(format!("known:{run}"), String::new());
        if observe_attempts(run, count, &mut seen, &mut events, |attempt| {
            let path = format!("actions/runs/{run}/attempts/{attempt}");
            let detail = object(config, &path, deadline)?;
            if id(&detail, "run_attempt")? != attempt {
                return Err("GitHub returned the wrong attempt".into());
            }
            let jobs = list(
                config,
                &format!("{path}/jobs?per_page=100"),
                Some("jobs"),
                deadline,
            )?;
            Ok(json!({"run":run,"number":attempt,"attempt":detail,"jobs":jobs}))
        })
        .is_err()
        {
            failed = true;
            break;
        }
    }
    Ok((json!(seen), events, failed))
}

pub(super) fn main(config: &Config, cursor: Value, deadline: Instant) -> Result<Observation> {
    let reference = object(config, "git/ref/heads/main", deadline)?;
    let tip = reference["object"]["sha"]
        .as_str()
        .filter(|s| super::local::sha(s))
        .ok_or("missing main tip")?;
    let runs = object(
        config,
        "actions/workflows/tests.yml/runs?branch=main&per_page=1",
        deadline,
    )?;
    let latest = runs["workflow_runs"]
        .as_array()
        .ok_or("missing main runs")?
        .first();
    let green = latest.is_some_and(|run| {
        run["head_sha"] == tip
            && run["head_branch"] == "main"
            && run["status"] == "completed"
            && run["conclusion"] == "success"
    });
    let status = json!({"tip":tip,"green":green,"run":latest});
    let events = if status != cursor {
        let mut event = Event::new("main", "full_run", status.clone());
        event.urgent = !green;
        vec![event]
    } else {
        vec![]
    };
    Ok((status, events, false))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn red_attempt_and_green_rerun_are_observed_independently() {
        let mut seen = BTreeMap::new();
        let mut events = Vec::new();
        let complete = |number| {
            let state = if number == 1 { "failure" } else { "success" };
            Ok(json!({"attempt":{"status":"completed","conclusion":state},
                      "jobs":[{"id":number,"status":"completed","conclusion":state}]}))
        };
        observe_attempts(1, 2, &mut seen, &mut events, complete).unwrap();
        assert_eq!(
            events
                .iter()
                .map(|e| e.data["jobs"][0]["conclusion"].as_str().unwrap())
                .collect::<Vec<_>>(),
            ["failure", "success"]
        );
        events.clear();
        observe_attempts(1, 2, &mut seen, &mut events, |_| {
            panic!("completed attempt reread")
        })
        .unwrap();
        assert!(events.is_empty());
    }
}
