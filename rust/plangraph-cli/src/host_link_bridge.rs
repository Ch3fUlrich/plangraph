//! Deliver host-link outbox events to the keeper message bus.
use serde_json::{json, Value};
use std::{
    env, fs,
    io::{self, Read, Seek, Write},
    process::{Command, Stdio},
    time::Duration,
};

fn fallback(event: &[u8]) -> Result<(), Box<dyn std::error::Error>> {
    let argv: Vec<String> = serde_json::from_str(&env::var("PLANGRAPH_WATCHDOG_FALLBACK")?)?;
    let (program, args) = argv.split_first().ok_or("empty watchdog fallback argv")?;
    let mut input = tempfile::tempfile()?;
    input.write_all(event)?;
    input.rewind()?;
    if !Command::new(program)
        .args(args)
        .stdin(Stdio::from(input))
        .status()?
        .success()
    {
        return Err("watchdog fallback failed".into());
    }
    Ok(())
}

fn deliver(event: &Value, rule: &str) -> Result<(), Box<dyn std::error::Error>> {
    let text = |key: &str| {
        event[key]
            .as_str()
            .filter(|value| !value.is_empty())
            .ok_or_else(|| format!("host-link event missing {key}"))
    };
    let host = text("host")?;
    let peer = text("peer")?;
    let msg_id = text("event_id")?;
    let since = event["down_since"]
        .as_u64()
        .ok_or("host-link event missing down_since")?;
    let summary = match rule {
        "host-link-down" => format!("Host link from {host} to {peer} unreachable since {since}"),
        "host-ssh-down" => format!(
            "{host} unreachable since {since}, peer {peer} state {}, last local IP change {}",
            event["peer_state"], event["last_ip_change"],
        ),
        "host-ssh-recovered" => format!("{host} recovered from the SSH outage since {since}"),
        _ => unreachable!(),
    };
    let url = env::var("PG_URL")?;
    let token = fs::read_to_string(env::var_os("PG_TOKEN_FILE").ok_or("missing PG_TOKEN_FILE")?)?;
    let sender = env::var("PG_HOST_LINK_SENDER")?;
    let agent: ureq::Agent = ureq::Agent::config_builder()
        .proxy(None)
        .max_redirects(0)
        .timeout_global(Some(Duration::from_secs(10)))
        .build()
        .into();
    let mut response = agent
        .post(format!("{}/v1/post_message", url.trim_end_matches('/')))
        .header("Authorization", format!("Bearer {}", token.trim()))
        .header("Connection", "close")
        .send_json(json!({
            "from": sender, "to": text("recipient")?, "key": text("item_key")?,
            "msg_id": msg_id, "body": { "text": summary },
        }))
        .map_err(|error| match error {
            ureq::Error::StatusCode(code) => format!("keeper returned HTTP {code}"),
            _ => "keeper delivery failed".into(),
        })?;
    let receipt: Value = response
        .body_mut()
        .with_config()
        .limit(64 * 1024)
        .read_json()?;
    if receipt["row"]["msg_id"] != msg_id {
        return Err("keeper response did not acknowledge host-link event ID".into());
    }
    Ok(())
}

fn run() -> Result<(), Box<dyn std::error::Error>> {
    const MAX_EVENT: u64 = 1024 * 1024;
    let mut bytes = Vec::new();
    io::stdin().take(MAX_EVENT + 1).read_to_end(&mut bytes)?;
    if bytes.len() as u64 > MAX_EVENT {
        return Err("watchdog event too large".into());
    }
    let event: Value = serde_json::from_slice(&bytes)?;
    match (event["kind"].as_str(), event["rule"].as_str()) {
        (
            Some("alert"),
            Some(rule @ ("host-link-down" | "host-ssh-down" | "host-ssh-recovered")),
        ) => deliver(&event, rule),
        _ => fallback(&bytes),
    }
}

pub fn cli() -> i32 {
    match run() {
        Ok(()) => 0,
        Err(error) => {
            eprintln!("host-link-deliver: {error}");
            1
        }
    }
}
