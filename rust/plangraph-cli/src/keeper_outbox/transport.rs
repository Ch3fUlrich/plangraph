use super::{Entry, Queue};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::io;
use std::path::PathBuf;
use std::time::Duration;

pub fn run(args: &[String]) -> i32 {
    match execute(args) {
        Ok(value) => {
            println!("{value}");
            0
        }
        Err(error) => {
            eprintln!("keeper outbox: {error}");
            4
        }
    }
}

fn execute(args: &[String]) -> io::Result<Value> {
    if let [command, id, decision] = args {
        if command == "resolve" {
            let path = std::env::var_os("PG_OUTBOX")
                .ok_or_else(|| io::Error::other("resolution requires the run's PG_OUTBOX"))?;
            Queue::open(&PathBuf::from(path))?.resolve(id, decision)?;
            return Ok(json!({"id": id, "decision": decision}));
        }
    }
    let (url, verb) = match args {
        [command, url, verb] if command == "write" => (url, Some(verb.as_str())),
        [command, url] if command == "replay" => (url, None),
        _ => return Err(io::Error::other("invalid outbox command")),
    };
    let token_file = std::env::var_os("PG_TOKEN_FILE")
        .ok_or_else(|| io::Error::other("PG_TOKEN_FILE is not set"))?;
    let token = std::fs::read_to_string(token_file)?;
    let fingerprint = format!("{:x}", Sha256::digest(token.trim()));
    let queue = Queue::open(&queue_path(&fingerprint)?)?;
    queue.bind_credential(&fingerprint)?;
    let transport = Transport::new(url, token.trim());
    let prepare = |entry: &Entry| transport.bind_submit(&queue, entry);
    let send = |entry: &Entry| transport.send(entry);
    let Some(verb) = verb else {
        return queue.drain(None, prepare, send).map(Value::Array);
    };
    if !matches!(verb, "heartbeat" | "add_evidence" | "submit") {
        return Err(io::Error::other("unsupported keeper write"));
    }
    let mut body: Value = serde_json::from_reader(io::stdin().lock())?;
    if verb == "submit" && body.get("source_round").is_none() {
        if let Ok(source) = std::env::var("PG_SOURCE_ROUND") {
            let round = source
                .parse::<u64>()
                .map_err(|_| io::Error::other("invalid PG_SOURCE_ROUND"))?;
            body["source_round"] = json!(round);
        }
    }
    let id = queue.enqueue(verb, body)?;
    let mut results = queue
        .drain(Some(&id), prepare, send)
        .map_err(|error| io::Error::other(format!("{verb} queued ({id}): {error}")))?;
    results
        .pop()
        .ok_or_else(|| io::Error::other(format!("{verb} queued ({id})")))
}

fn queue_path(fingerprint: &str) -> io::Result<PathBuf> {
    if let Some(path) = std::env::var_os("PG_OUTBOX").filter(|path| !path.is_empty()) {
        return Ok(PathBuf::from(path));
    }
    let state = std::env::var_os("XDG_STATE_HOME")
        .map(PathBuf::from)
        .or_else(|| std::env::var_os("HOME").map(|home| PathBuf::from(home).join(".local/state")))
        .ok_or_else(|| io::Error::other("no local state directory"))?;
    Ok(state.join("plangraph/outbox").join(fingerprint))
}

impl Queue {
    fn source_round(&self, key: &str, epoch: &Value) -> io::Result<Option<u64>> {
        let _entries = crate::atomic::lock(&self.root.join("entries"))?;
        Ok(super::read_entries(&self.done)?
            .iter()
            .rev()
            .find_map(|(_, entry)| {
                if entry.legacy || entry.body["key"] != key || &entry.body["epoch"] != epoch {
                    return None;
                }
                entry.body["source_round"].as_u64().or_else(|| {
                    entry
                        .outcome
                        .as_ref()?
                        .get("reply")?
                        .get("source_round")?
                        .as_u64()
                })
            }))
    }
}

struct Transport<'a> {
    agent: ureq::Agent,
    url: &'a str,
    authorization: String,
}

impl<'a> Transport<'a> {
    fn new(url: &'a str, token: &str) -> Self {
        let config = ureq::Agent::config_builder()
            .timeout_global(Some(Duration::from_secs(15)))
            .max_redirects(0)
            .build();
        Self {
            agent: ureq::Agent::new_with_config(config),
            url: url.trim_end_matches('/'),
            authorization: format!("Bearer {token}"),
        }
    }

    fn bind_submit(&self, queue: &Queue, entry: &Entry) -> io::Result<Option<Value>> {
        if entry.verb != "submit" || entry.body.get("source_round").is_some() {
            return Ok(None);
        }
        let key = entry.body["key"]
            .as_str()
            .ok_or_else(|| io::Error::other("submit requires a key"))?;
        let epoch = &entry.body["epoch"];
        let round = match queue.source_round(key, epoch)? {
            Some(round) => round,
            None => {
                let context = self.context(key)?;
                if &context["epoch"] != epoch {
                    return Err(io::Error::other("keeper claim epoch changed before submit"));
                }
                context["source_round"]
                    .as_u64()
                    .ok_or_else(|| io::Error::other("keeper did not supply a source round"))?
            }
        };
        let mut body = entry.body.clone();
        body["source_round"] = json!(round);
        Ok(Some(body))
    }

    fn send(&self, entry: &Entry) -> io::Result<Value> {
        self.agent
            .post(format!("{}/v1/write_receipt", self.url))
            .header("Authorization", &self.authorization)
            .send_json(entry)
            .map_err(http_error)?
            .body_mut()
            .read_json()
            .map_err(http_error)
    }

    fn context(&self, key: &str) -> io::Result<Value> {
        self.agent
            .get(format!("{}/v1/write_context", self.url))
            .query("key", key)
            .header("Authorization", &self.authorization)
            .call()
            .map_err(http_error)?
            .body_mut()
            .read_json()
            .map_err(http_error)
    }
}

fn http_error(error: ureq::Error) -> io::Error {
    match error {
        ureq::Error::StatusCode(status) => io::Error::other(format!("keeper HTTP {status}")),
        _ => io::Error::other("keeper transport did not return a receipt"),
    }
}
