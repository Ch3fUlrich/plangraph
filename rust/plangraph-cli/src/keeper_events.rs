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

pub(crate) struct Client {
    url: String,
    token: String,
    agent: ureq::Agent,
}

impl Client {
    pub(crate) fn new(env: &BTreeMap<String, String>) -> Result<Self, String> {
        Self::with_token_file(env, "PG_TOKEN_FILE")
    }

    pub(crate) fn with_token_file(
        env: &BTreeMap<String, String>,
        key: &str,
    ) -> Result<Self, String> {
        let url = env.get("PG_URL").ok_or("PG_URL required")?;
        let token_file = env.get(key).ok_or_else(|| format!("{key} required"))?;
        let token =
            std::fs::read_to_string(token_file).map_err(|_| "cannot read keeper token file")?;
        let agent = ureq::Agent::config_builder()
            .proxy(None)
            .max_redirects(0)
            .timeout_global(Some(Duration::from_secs(20)))
            .build()
            .into();
        Ok(Self {
            url: url.trim_end_matches('/').into(),
            token,
            agent,
        })
    }

    pub(crate) fn get<T: serde::de::DeserializeOwned>(&self, path: &str) -> Result<T, String> {
        self.agent
            .get(format!("{}/{path}", self.url))
            .header("Authorization", format!("Bearer {}", self.token.trim()))
            .call()
            .map_err(|error| match error {
                ureq::Error::StatusCode(status) => format!("keeper returned HTTP {status}"),
                _ => "keeper request failed".into(),
            })?
            .body_mut()
            .read_json()
            .map_err(|_| "invalid keeper response".into())
    }

    pub(crate) fn post(
        &self,
        path: &str,
        idempotency_key: &str,
        body: &Value,
    ) -> Result<Value, String> {
        self.agent
            .post(format!("{}/{path}", self.url))
            .header("Authorization", format!("Bearer {}", self.token.trim()))
            .header("Idempotency-Key", idempotency_key)
            .send_json(body)
            .map_err(|error| match error {
                ureq::Error::StatusCode(status) => format!("keeper returned HTTP {status}"),
                _ => "keeper request failed".into(),
            })?
            .body_mut()
            .read_json()
            .map_err(|_| "invalid keeper response".into())
    }

    pub(crate) fn events(&self, since: u64, limit: u16) -> Result<Page, String> {
        self.get(&format!("v1/events?since={since}&limit={limit}"))
    }
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
    Client::new(env)?.events(since.unwrap_or(0), limit.unwrap_or(100))
}
