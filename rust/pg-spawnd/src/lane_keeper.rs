//! Keeper's open-lane read of an item: the branches the Keeper host sees for a key, which
//! another host may have pushed before this checkout fetched them.

use crate::lane_guard::Lane;
use serde_json::Value;
use std::{
    fs,
    io::Write,
    process::{Command, Stdio},
};

/// Reads `show` for an item key from the Keeper.
pub type Fetch = Box<dyn Fn(&str) -> Result<Value, String> + Send + Sync>;

/// The open lanes of `show`'s `work_exists.lanes`; `Err` when the Keeper could not tell
/// (`lanes: null`) so the caller says the answer is partial instead of treating it as empty.
pub fn lanes_of(key: &str, show: &Value) -> Result<Vec<Lane>, String> {
    let lanes = &show["work_exists"]["lanes"];
    let Some(names) = lanes.as_array() else {
        return Err(format!("keeper lanes of {key} unknown"));
    };
    Ok(names
        .iter()
        .filter_map(Value::as_str)
        .map(|name| Lane {
            name: name.to_owned(),
            keys: vec![key.to_owned()],
            source: "keeper",
        })
        .collect())
}

/// The lanes the Keeper reports for `keys`, minus the branch this checkout works on. A key
/// the Keeper cannot answer is reported on stderr; the other sources still apply.
pub fn lanes(fetch: &Fetch, keys: &[String], own: Option<&str>) -> Vec<Lane> {
    keys.iter()
        .flat_map(|key| {
            fetch(key)
                .and_then(|show| lanes_of(key, &show))
                .unwrap_or_else(|error| {
                    eprintln!("lane guard: warning: {error}");
                    Vec::new()
                })
        })
        .filter(|lane| own != Some(lane.name.as_str()))
        .collect()
}

/// The Keeper named by `PG_URL` and `PG_TOKEN_FILE`; none when this host has no Keeper access.
pub fn from_env() -> Option<Fetch> {
    let url = std::env::var("PG_URL").ok().filter(|url| !url.is_empty())?;
    let token_file = std::env::var("PG_TOKEN_FILE").ok()?;
    Some(Box::new(move |key| show(&url, &token_file, key)))
}

fn show(url: &str, token_file: &str, key: &str) -> Result<Value, String> {
    let token = fs::read_to_string(token_file).map_err(|_| "keeper token file unreadable")?;
    let token = token.trim();
    if token.is_empty()
        || !token
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"-_.=+/".contains(&b))
    {
        return Err("invalid keeper token".into());
    }
    let mut child = Command::new("curl")
        .args([
            "--silent",
            "--fail",
            "--max-time",
            "15",
            "--proto",
            "=http,https",
            "--config",
            "-",
            "--get",
            "--data-urlencode",
        ])
        .arg(format!("key={key}"))
        .args(["--data-urlencode", "fmt=json"])
        .arg(format!("{}/v1/show", url.trim_end_matches('/')))
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .map_err(|_| "cannot execute curl")?;
    child
        .stdin
        .take()
        .ok_or("curl stdin unavailable")?
        .write_all(format!("header = \"Authorization: Bearer {token}\"\n").as_bytes())
        .map_err(|_| "keeper request failed")?;
    let out = child
        .wait_with_output()
        .map_err(|_| "keeper request failed")?;
    if !out.status.success() {
        return Err(format!("keeper unreachable for {key}"));
    }
    serde_json::from_slice(&out.stdout).map_err(|_| "keeper returned invalid JSON".into())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn fetch(show: Value) -> Fetch {
        Box::new(move |_| Ok(show.clone()))
    }

    #[test]
    fn keeper_open_lanes_are_lanes_of_the_key() {
        let show = json!({"work_exists":{"commits":[],"lanes":["lane/other-host","lane/mine"]}});
        let keys = ["demo:K".to_owned()];
        let found = lanes(&fetch(show), &keys, Some("lane/mine"));
        assert_eq!(
            found,
            [Lane {
                name: "lane/other-host".into(),
                keys: vec!["demo:K".into()],
                source: "keeper"
            }]
        );
    }

    #[test]
    fn an_unknown_answer_is_an_error_not_an_empty_lane_list() {
        let show = json!({"work_exists":{"commits":[],"lanes":null}});
        assert!(lanes_of("demo:K", &show).is_err());
        let failing: Fetch = Box::new(|_| Err("keeper unreachable for demo:K".into()));
        assert!(lanes(&failing, &["demo:K".into()], None).is_empty());
    }
}
