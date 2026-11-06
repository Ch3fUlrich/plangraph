use pg_worker_core::{placement_rule_bridge::handle, placement_rule_store::StoreError};
use serde_json::{json, Value};
use std::io::{Read, Write};

const MAX_INPUT: u64 = 1024 * 1024;

fn request() -> Result<Value, StoreError> {
    if std::env::args_os().len() != 1 {
        return Err(StoreError::Invalid);
    }
    let mut bytes = Vec::new();
    std::io::stdin()
        .take(MAX_INPUT + 1)
        .read_to_end(&mut bytes)
        .map_err(|_| StoreError::Io)?;
    if bytes.len() as u64 > MAX_INPUT {
        return Err(StoreError::Invalid);
    }
    handle(serde_json::from_slice(&bytes).map_err(|_| StoreError::Invalid)?)
}

fn main() {
    let (response, code) = match request() {
        Ok(value) => (json!({"ok": value}), 0),
        Err(error) => (json!({"error": error.to_string()}), 2),
    };
    let mut stdout = std::io::stdout().lock();
    if writeln!(stdout, "{response}").is_err() {
        std::process::exit(2);
    }
    std::process::exit(code);
}

#[cfg(test)]
#[path = "pg-placement-rules_tests.rs"]
mod tests;
