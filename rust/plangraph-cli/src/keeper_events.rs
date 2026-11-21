//! Bounded, authenticated reads of the keeper event feed.
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::BTreeMap;
use std::time::Duration;

#[derive(Debug, Deserialize, Serialize)]
pub struct Page {
    pub rows: Vec<Value>,
    pub next_cursor: u64,
    pub more: bool,
}

pub fn read(env: &BTreeMap<String, String>, since: u64, limit: u16) -> Result<Page, String> {
    let url = env.get("PG_URL").ok_or("PG_URL required")?;
    let token_file = env.get("PG_TOKEN_FILE").ok_or("PG_TOKEN_FILE required")?;
    let token = std::fs::read_to_string(token_file).map_err(|_| "cannot read keeper token file")?;
    let agent: ureq::Agent = ureq::Agent::config_builder()
        .proxy(None)
        .max_redirects(0)
        .timeout_global(Some(Duration::from_secs(20)))
        .build()
        .into();
    let endpoint = format!(
        "{}/v1/events?since={since}&limit={limit}",
        url.trim_end_matches('/')
    );
    let mut response = agent
        .get(endpoint)
        .header("Authorization", format!("Bearer {}", token.trim()))
        .call()
        .map_err(|error| match error {
            ureq::Error::StatusCode(status) => format!("keeper events returned HTTP {status}"),
            _ => "keeper events request failed".into(),
        })?;
    response
        .body_mut()
        .read_json()
        .map_err(|_| "invalid keeper events response".into())
}

pub fn cli(env: &BTreeMap<String, String>, args: &[String]) -> Result<Page, String> {
    let mut since = None;
    let mut limit = None;
    for pair in args.chunks(2) {
        match pair {
            [flag, value] if flag == "--since" && since.is_none() => {
                since = Some(value.parse().map_err(|_| "invalid events cursor")?);
            }
            [flag, value] if flag == "--limit" && limit.is_none() => {
                limit = Some(value.parse().map_err(|_| "invalid events limit")?);
            }
            _ => return Err("usage: plangraph keeper events [--since SEQ] [--limit N]".into()),
        }
    }
    read(env, since.unwrap_or(0), limit.unwrap_or(100))
}
