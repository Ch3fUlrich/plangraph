use pg_worker_core::run::{lifecycle, record};
use serde_json::Value;
use std::io::{Read, Write};

pub(crate) fn execute(verb: &str, args: &[String], out: &mut dyn Write) -> Result<(), String> {
    let state = crate::state::resolve_state(None, |name| std::env::var_os(name))?;
    let result = match (verb, args) {
        ("runs", [flag, cursor]) if flag == "--changed-since" => {
            let cursor = cursor.parse().map_err(|_| "invalid run lifecycle cursor")?;
            let page = lifecycle::page(&state, cursor).map_err(|error| error.to_string())?;
            writeln!(out, "{page}")
        }
        ("record-publish", []) => {
            let mut bytes = Vec::new();
            let mut stdin = std::io::stdin().take(16 * 1024 * 1024 + 1);
            stdin
                .read_to_end(&mut bytes)
                .map_err(|_| "record input failed")?;
            if bytes.len() > 16 * 1024 * 1024 {
                return Err("record input too large".into());
            }
            let value: Value = serde_json::from_slice(&bytes).map_err(|_| "invalid record JSON")?;
            let id = value["run_id"].as_str().ok_or("missing run id")?;
            let path = record::run_dir(&state, id).map_err(|error| error.to_string())?;
            lifecycle::publish(&state, &path.join("record.json"), &value)
        }
        _ => return Err("usage: pg-spawnd runs --changed-since CURSOR | record-publish".into()),
    };
    result.map_err(|error| error.to_string())
}
