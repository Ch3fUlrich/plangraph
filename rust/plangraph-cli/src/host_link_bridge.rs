//! Deliver host-link outbox events to the keeper message bus.
use pg_worker_core::host_link::{DISK_HYGIENE_BELOW_ALARM, DISK_HYGIENE_ZERO_PROGRESS};
use serde_json::{json, Value};
use std::{
    env,
    fmt::Write as _,
    fs,
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

fn display_disk_path(path: &str) -> String {
    const LIMIT: usize = 384;
    let mut encoded = String::new();
    for byte in path.bytes() {
        match byte {
            0..=31 | 127..=255 | b'"' | b'\'' | b'\\' => write!(encoded, "%{byte:02X}").unwrap(),
            _ => encoded.push(char::from(byte)),
        }
    }
    if encoded.len() <= LIMIT {
        return encoded;
    }
    let basename = encoded.rsplit('/').next().unwrap();
    let tail = ((LIMIT - 3) / 2).max((basename.len() + 1).min(LIMIT - 3 - 32));
    let (mut left, mut right) = (LIMIT - 3 - tail, encoded.len() - tail);
    // Paths from the outbox may already contain percent escapes for raw path bytes.
    let boundary = |index: usize| {
        (1..=2).all(|back| {
            let field = &encoded.as_bytes()[index - back..index - back + 3];
            field[0] != b'%' || !field[1..].iter().all(u8::is_ascii_hexdigit)
        })
    };
    while !boundary(left) {
        left -= 1;
    }
    while !boundary(right) {
        right += 1;
    }
    format!("{}...{}", &encoded[..left], &encoded[right..])
}

fn disk_pressure_text(event: &Value, rule: &str, host: &str) -> Option<String> {
    let device = event["device"].as_u64()?;
    let free = event["end_free_bytes"].as_u64()?;
    let floor = event["floor_bytes"].as_u64()?;
    let freed = event["freed_bytes"].as_u64()?;
    let host = display_disk_path(host);
    let mut summary = format!("{rule} on {host} (device {device}, free {free} bytes, floor {floor} bytes, reclaimed {freed} bytes)");
    let rows = event["kept"].as_array()?;
    for row in rows.iter().take(5) {
        let path = display_disk_path(row["path"].as_str()?);
        let reason = display_disk_path(row["reason"].as_str()?);
        write!(summary, "\n{reason} {path}").unwrap();
    }
    Some(summary)
}

fn deliver(event: &Value, rule: &str) -> Result<(), Box<dyn std::error::Error>> {
    let text = |key: &str| {
        event[key]
            .as_str()
            .filter(|value| !value.is_empty())
            .ok_or_else(|| format!("host-link event missing {key}"))
    };
    let host = text("host")?;
    let msg_id = text("event_id")?;
    let since = event["down_since"]
        .as_u64()
        .ok_or("host-link event missing down_since")?;
    let mut worker_host = None;
    let summary = match rule {
        DISK_HYGIENE_ZERO_PROGRESS | DISK_HYGIENE_BELOW_ALARM => {
            disk_pressure_text(event, rule, host).ok_or("invalid disk hygiene event")?
        }
        "host-link-down" => format!(
            "Host link from {host} to {} unreachable since {since}",
            text("peer")?
        ),
        "host-ssh-down" => format!(
            "{host} unreachable since {since}, peer {} state {}, last local IP change {}",
            text("peer")?,
            event["peer_state"],
            event["last_ip_change"],
        ),
        "host-ssh-recovered" => {
            text("peer")?;
            format!("{host} recovered from the SSH outage since {since}")
        }
        "worker-host-unreachable" | "worker-host-maintenance" | "worker-host-recovered" => {
            let status = rule.strip_prefix("worker-host-").expect("worker host rule");
            let runs = &event["unknown_run_ids"];
            let until = &event["maintenance_until"];
            worker_host = Some(json!({
                "host": host, "status": status, "observed_at": event["observed_at"],
                "down_since": since, "unknown_run_ids": runs, "maintenance_until": until,
            }));
            format!("{host} {status} (outage since {since}, maintenance end {until}); runs to check {runs}")
        }
        _ => unreachable!(),
    };
    let mut body = json!({ "text": summary });
    if matches!(rule, DISK_HYGIENE_ZERO_PROGRESS | DISK_HYGIENE_BELOW_ALARM)
        && serde_json::to_vec(&body)?.len() + 1 >= 4096
    {
        return Err("disk hygiene alert body is too large".into());
    }
    if let Some(event) = worker_host {
        body["worker_host"] = event;
    }
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
            "msg_id": msg_id, "body": body,
        }))
        .map_err(|error| -> Box<dyn std::error::Error> {
            match error {
                status @ ureq::Error::StatusCode(_) => status.into(),
                _ => "keeper delivery failed".into(),
            }
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

fn run(input: impl Read) -> Result<(), Box<dyn std::error::Error>> {
    const MAX_EVENT: u64 = 1024 * 1024;
    let mut bytes = Vec::new();
    input.take(MAX_EVENT + 1).read_to_end(&mut bytes)?;
    if bytes.len() as u64 > MAX_EVENT {
        return Err("watchdog event too large".into());
    }
    let event: Value = serde_json::from_slice(&bytes)?;
    match (event["kind"].as_str(), event["rule"].as_str()) {
        (
            Some("alert"),
            Some(
                rule @ ("host-link-down"
                | "host-ssh-down"
                | "host-ssh-recovered"
                | "worker-host-unreachable"
                | "worker-host-maintenance"
                | "worker-host-recovered"
                | DISK_HYGIENE_ZERO_PROGRESS
                | DISK_HYGIENE_BELOW_ALARM),
            ),
        ) => deliver(&event, rule),
        _ => fallback(&bytes),
    }
}

pub fn cli(args: &[String]) -> i32 {
    let result = match args {
        [] => run(io::stdin()),
        [path] => fs::File::open(path).map_err(Into::into).and_then(run),
        _ => Err("usage: plangraph host-link-deliver [EVENT_PATH]".into()),
    };
    match result {
        Ok(()) => 0,
        Err(error) => {
            if let Some(ureq::Error::StatusCode(status)) = error.downcast_ref::<ureq::Error>() {
                eprintln!("host-link-deliver: keeper returned HTTP {status}");
                return refusal_exit(*status, args.len() == 1);
            }
            eprintln!("host-link-deliver: {error}");
            1
        }
    }
}

fn refusal_exit(status: u16, file_delivery: bool) -> i32 {
    if file_delivery && matches!(status, 400 | 413 | 422) {
        pg_worker_core::host_link::WORKER_ALERT_REFUSED_EXIT
    } else {
        1
    }
}

#[cfg(test)]
mod tests {
    use super::refusal_exit;
    use pg_worker_core::host_link::WORKER_ALERT_REFUSED_EXIT;

    #[test]
    fn only_permanent_file_payload_refusals_are_quarantinable() {
        for status in [400, 413, 422] {
            assert_eq!(refusal_exit(status, true), WORKER_ALERT_REFUSED_EXIT);
            assert_eq!(refusal_exit(status, false), 1);
        }
        for status in [401, 403, 404, 409, 429, 500, 503] {
            assert_eq!(refusal_exit(status, true), 1);
        }
    }
}
