use super::{github, local, Config, Event, Observation, Result};
use crate::keeper_events::Client;
use serde::Deserialize;
use serde_json::{json, Value};
use std::io::{Read, Seek, SeekFrom};
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

const TIMEOUT: Duration = Duration::from_secs(20);
pub(super) const CAP: u64 = 8 * 1024 * 1024;

pub(super) fn command(mut command: Command, deadline: Instant) -> Result<String> {
    let buffer = tempfile::tempfile().map_err(|_| "cannot create source buffer")?;
    let mut read = buffer
        .try_clone()
        .map_err(|_| "cannot open source buffer")?;
    let mut child = command
        .stdin(Stdio::null())
        .stderr(Stdio::null())
        .stdout(buffer)
        .spawn()
        .map_err(|_| "cannot start source")?;
    loop {
        match child.try_wait() {
            Ok(Some(status)) if status.success() => break,
            Ok(Some(_)) => return Err("source command failed".into()),
            _ if Instant::now() >= deadline => {
                let _ = child.kill();
                let _ = child.wait();
                return Err("source timeout".into());
            }
            _ => std::thread::sleep(Duration::from_millis(20)),
        }
    }
    read.seek(SeekFrom::Start(0))
        .map_err(|_| "source seek failed")?;
    let mut text = String::new();
    read.take(CAP + 1)
        .read_to_string(&mut text)
        .map_err(|_| "invalid source output")?;
    if text.len() as u64 > CAP {
        return Err("source output exceeds limit".into());
    }
    Ok(text)
}

#[derive(Deserialize)]
pub(super) struct Identity {
    name: String,
    role: String,
    projects: Vec<String>,
}

impl Identity {
    fn includes_item(&self, key: &str) -> bool {
        self.role == "router"
            || key.split_once(':').is_some_and(|(project, _)| {
                self.projects.iter().any(|candidate| candidate == project)
            })
    }
}

pub(super) fn authorize(config: &Config, keeper: &Client) -> Result<Identity> {
    let identity: Identity = keeper.get("v1/whoami")?;
    if identity.name != config.principal {
        return Err("keeper token does not match the configured role principal".into());
    }
    if config
        .item_keys
        .iter()
        .any(|key| !identity.includes_item(key))
    {
        return Err("configured run item is outside the keeper token project scope".into());
    }
    Ok(identity)
}

fn rows<'a>(value: &'a Value, field: &str) -> Result<&'a [Value]> {
    value[field]
        .as_array()
        .map(Vec::as_slice)
        .ok_or_else(|| format!("missing {field} rows"))
}

fn in_scope(config: &Config, identity: &Identity, row: &Value) -> bool {
    identity.role == "router"
        || row["role"] == config.role
        || row["item_key"]
            .as_str()
            .is_some_and(|key| identity.includes_item(key))
}

fn messages(config: &Config, value: &Value, mut cursor: u64) -> Result<Observation> {
    let mut events = Vec::new();
    for row in rows(value, "rows")? {
        let seq = row["seq"]
            .as_u64()
            .filter(|seq| *seq > cursor)
            .ok_or("invalid message sequence")?;
        cursor = seq;
        if row["to"] == config.principal || row["to"] == config.role {
            let mut event = Event::new("messages", "message", row.clone());
            event.untrusted = true;
            event.urgent = urgent_message(&row["body"].to_string());
            events.push(event);
        }
    }
    Ok((json!(cursor), events, false))
}

pub(super) fn urgent_message(text: &str) -> bool {
    text.split(|c: char| !c.is_ascii_alphabetic())
        .any(|word| word == "BLOCKED" || word == "FAIL")
}

pub(super) fn level(source: &str, kind: &str, current: Value, previous: Value) -> Observation {
    let row = (current != previous).then(|| Event::new(source, kind, current.clone()));
    (current, row.into_iter().collect(), false)
}

fn item_levels(config: &Config, value: &Value, previous: Value, now: i64) -> Result<Observation> {
    let mut held = Vec::new();
    let mut margins = Vec::new();
    for row in rows(value, "items")? {
        let key = &row["key"];
        if row["claimed_by"] == config.principal
            && matches!(row["state"].as_str(), Some("claimed" | "in_review"))
        {
            held.push(json!({"key":key,"state":row["state"],"epoch":row["epoch"],"lease_until":row["lease_until"]}));
            if row["lease_until"]
                .as_str()
                .and_then(|ts| chrono::DateTime::parse_from_rfc3339(ts).ok())
                .is_some_and(|ts| ts.timestamp() - now <= config.lease_margin_seconds)
            {
                margins.push(json!({"key":key,"lease_until":row["lease_until"]}));
            }
        }
    }
    let state = json!({"held":held, "margins":margins});
    let (state, mut events, _) = level("items", "levels", state, previous.clone());
    for margin in &margins {
        if !previous["margins"]
            .as_array()
            .is_some_and(|rows| rows.contains(margin))
        {
            let mut event = Event::new("items", "lease_margin", margin.clone());
            event.urgent = true;
            events.push(event);
        }
    }
    Ok((state, events, false))
}

fn read_runs(
    config: &Config,
    identity: &Identity,
    cursor: Value,
    execute: impl FnOnce(Command) -> Result<String>,
) -> Result<Observation> {
    let mut cmd = Command::new("plangraph-spawn");
    let cursor = if cursor.is_null() {
        "{}".to_owned()
    } else {
        cursor.to_string()
    };
    cmd.args(["runs", "--changed-since", &cursor]);
    let value: Value = serde_json::from_str(&execute(cmd)?).map_err(|_| "invalid runs response")?;
    let hosts = value["hosts"].as_object().ok_or("invalid host page")?;
    let mut events = Vec::new();
    let mut failed = false;
    for (host, page) in std::iter::once(("local", &value["local"]))
        .chain(hosts.iter().map(|(host, page)| (host.as_str(), page)))
    {
        if page.get("error").is_some() {
            failed = true;
            continue;
        }
        for row in rows(page, "rows")? {
            if in_scope(config, identity, row) {
                let mut event = Event::new(
                    "runs",
                    row["phase"].as_str().ok_or("missing run phase")?,
                    json!({"source_host":host, "transition":row}),
                );
                event.urgent = matches!(
                    row["status"].as_str(),
                    Some("failed" | "timeout" | "orphaned" | "unknown" | "stalled")
                );
                events.push(event);
            }
        }
    }
    if !value["next_cursor"].is_object() {
        return Err("missing run lifecycle bookmark".into());
    }
    Ok((value["next_cursor"].clone(), events, failed))
}

pub(super) fn read(
    source: &str,
    config: &Config,
    keeper: &Client,
    cursor: Value,
) -> Result<Observation> {
    let deadline = Instant::now() + TIMEOUT;
    let seq = cursor.as_u64().unwrap_or(0);
    match source {
        "keeper" => {
            let page = keeper.events(seq, 500)?;
            let next = page.next_cursor;
            if next < seq
                || page.rows.iter().any(|row| {
                    row["seq"]
                        .as_u64()
                        .is_none_or(|number| number <= seq || number > next)
                })
            {
                return Err("invalid event bookmark".into());
            }
            let rows = page
                .rows
                .iter()
                .map(|row| {
                    let kind = row["type"].as_str().unwrap_or("event");
                    let mut event = Event::new(source, kind, row.clone());
                    event.urgent = matches!(
                        kind,
                        "answered" | "apply_granted" | "question_resolved" | "lease_expired"
                    );
                    event
                })
                .collect();
            Ok((json!(next), rows, false))
        }
        "messages" => messages(
            config,
            &keeper.get(&format!("v1/messages?since={seq}"))?,
            seq,
        ),
        "items" => item_levels(
            config,
            &keeper.get(
                "v1/snapshot?open=1&blocks=items&fields=key,state,epoch,claimed_by,lease_until",
            )?,
            cursor,
            chrono::Utc::now().timestamp(),
        ),
        "hosts" => {
            let data = keeper.get("v1/hosts")?;
            let current = Value::Array(
                rows(&data, "hosts")?
                    .iter()
                    .map(|row| {
                        json!({
                            "host":row["host"],"health":row["health"],
                        })
                    })
                    .collect(),
            );
            Ok(level(source, "levels", current, cursor))
        }
        "quota" => local::quota(config, cursor),
        "ready" => {
            let data = keeper.get("v1/ready?fmt=json&mine=true&all=1")?;
            let current = Value::Array(
                rows(&data, "items")?
                    .iter()
                    .filter(|row| row["owner"] == config.principal)
                    .map(|row| row["key"].clone())
                    .collect(),
            );
            Ok(level(source, "levels", current, cursor))
        }
        "runs" => read_runs(config, &authorize(config, keeper)?, cursor, |cmd| {
            command(cmd, deadline)
        }),
        "inbox" => local::lines(config, source, &config.inbox, cursor),
        "transcript" => local::lines(config, source, &config.transcript, cursor),
        "decisions" => local::decisions(config, cursor, deadline),
        "github" => github::history(config, cursor, deadline),
        "main" => github::main(config, cursor, deadline),
        _ => Err("unknown source".into()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn run_source_admits_project_and_role_scope_without_item_keys() {
        let mut config = super::super::fixture(std::path::Path::new("."));
        config.item_keys.clear();
        let mut identity = Identity {
            name: config.principal.clone(),
            role: "l1".into(),
            projects: vec!["demo".into()],
        };
        let page = json!({
            "local":{"rows":[
                {"phase":"ended","status":"failed","run_id":"new-item","item_key":"demo:NEW","role":"review"},
                {"phase":"ended","status":"completed","run_id":"private","item_key":"elsewhere:A","role":"review"}
            ]},
            "hosts":{"fixture":{"rows":[
                {"phase":"started","status":"running","run_id":"role-only","role":"routing"},
                {"phase":"started","status":"running","run_id":"unknown-scope","role":"review"}
            ]}},
            "next_cursor":{"local":2,"hosts":{"fixture":2}}
        });
        let execute = |cmd: Command| {
            assert_eq!(cmd.get_program(), "plangraph-spawn");
            assert_eq!(
                cmd.get_args().collect::<Vec<_>>(),
                ["runs", "--changed-since", "{}"]
            );
            Ok(page.to_string())
        };
        let (cursor, events, failed) = read_runs(&config, &identity, Value::Null, execute).unwrap();
        assert_eq!(cursor, page["next_cursor"]);
        assert!(!failed);
        assert_eq!(
            events
                .iter()
                .map(|row| row.data["transition"]["run_id"].as_str().unwrap())
                .collect::<Vec<_>>(),
            ["new-item", "role-only"]
        );
        assert!(events[0].urgent);
        identity.role = "router".into();
        identity.projects.clear();
        assert_eq!(
            read_runs(&config, &identity, Value::Null, execute)
                .unwrap()
                .1
                .len(),
            4
        );
    }

    #[test]
    fn records_lease_margin_crossing_without_a_write() {
        let config = super::super::fixture(std::path::Path::new("."));
        let snapshot = json!({"items":[
            {"key":"demo:A", "claimed_by":"routing-reader", "state":"claimed",
             "epoch":1, "lease_until":"1970-01-01T00:20:00Z"},
            {"key":"demo:PRIVATE", "claimed_by":"another-role", "state":"claimed",
             "epoch":1, "lease_until":"1970-01-01T00:20:00Z"}
        ]});
        let (before, events, _) = item_levels(&config, &snapshot, Value::Null, 599).unwrap();
        assert!(events.iter().all(|event| event.kind != "lease_margin"));
        let (after, events, _) = item_levels(&config, &snapshot, before, 600).unwrap();
        assert!(events
            .iter()
            .any(|event| event.kind == "lease_margin" && event.urgent));
        assert!(!after.to_string().contains("PRIVATE"));
        assert!(item_levels(&config, &snapshot, after, 601)
            .unwrap()
            .1
            .is_empty());
    }

    #[test]
    fn message_cursor_scans_past_rows_that_are_never_written() {
        let config = super::super::fixture(std::path::Path::new("."));
        let page = json!({"rows":[
            {"seq":1, "to":"routing-reader", "body":"BLOCKED in scope"},
            {"seq":2, "to":"routing-extra", "body":"private"}
        ]});
        let (cursor, events, _) = messages(&config, &page, 0).unwrap();
        assert_eq!(cursor, 2);
        assert_eq!(events.len(), 1);
        assert!(events[0].untrusted && events[0].urgent);
        let directory = tempfile::tempdir().unwrap();
        let mut checkpoint = super::super::Checkpoint {
            scan: 1,
            ..Default::default()
        };
        super::super::journal::commit(directory.path(), 0, &mut checkpoint, &events).unwrap();
        let content = std::fs::read_to_string(directory.path().join("events.jsonl")).unwrap();
        assert!(content.contains("in scope"));
        assert!(!content.contains("private"));
    }
}
