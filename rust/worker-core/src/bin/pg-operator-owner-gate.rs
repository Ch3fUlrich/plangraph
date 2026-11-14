use pg_worker_core::operator_owner_gate::handle;
use serde_json::json;
use std::io::{Read, Write};

fn main() {
    let mut bytes = Vec::new();
    let response = if std::env::args_os().len() != 1
        || std::io::stdin()
            .take(1024 * 1024 + 1)
            .read_to_end(&mut bytes)
            .is_err()
        || bytes.len() > 1024 * 1024
    {
        Err("invalid request")
    } else {
        serde_json::from_slice(&bytes)
            .map_err(|_| "invalid request")
            .and_then(handle)
    };
    let (body, code) = match response {
        Ok(value) => (json!({"ok": value}), 0),
        Err(_) => (json!({"error": "rules_invalid"}), 2),
    };
    if writeln!(std::io::stdout().lock(), "{body}").is_err() {
        std::process::exit(2);
    }
    std::process::exit(code);
}
