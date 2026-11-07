use serde_json::{json, Value};
use std::io::Write;
use std::process::{Command, Output, Stdio};

fn run(args: &[&str], body: &[u8]) -> Output {
    use std::os::unix::fs::OpenOptionsExt;
    let path = std::env::temp_dir().join(format!(
        "og-host-{}-{:?}",
        std::process::id(),
        std::thread::current().id()
    ));
    let mut file = std::fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(&path)
        .unwrap();
    write!(file, "offline-host").unwrap();
    let mut child = Command::new(env!("CARGO_BIN_EXE_og-memory"))
        .env("OG_MEMORY_BASE_URL", "http://127.0.0.1:1/graphs/local")
        .env("OG_MEMORY_TOKEN_FILE", &path)
        .args(args)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    // Oversize input may be refused before the writer finishes.
    let _ = child.stdin.take().unwrap().write_all(body);
    let output = child.wait_with_output().unwrap();
    std::fs::remove_file(path).unwrap();
    output
}
fn fact(project: &str) -> Vec<u8> {
    serde_json::to_vec(&json!({
        "version":1,"project":project,"kind":"Decision","slug":"example",
        "content":"Use typed boundaries", "actor":"test",
        "source":{"type":"file","path":"notes.md","hash":"example"},"relations":[]
    }))
    .unwrap()
}
fn refused(args: &[&str], body: &[u8], reason: &str) {
    let out = run(args, body);
    assert_eq!(out.status.code(), Some(1));
    assert_eq!(String::from_utf8(out.stderr).unwrap().trim(), reason);
    assert!(out.stdout.is_empty());
}
#[test]
fn binary_flags_and_envelope_errors() {
    for (args, reason) in [
        (vec![], "MissingOperation"),
        (vec!["recall", "--scope"], "MissingFlagValue"),
        (vec!["recall", "--unknown", "global"], "InvalidFlag"),
        (
            vec!["recall", "--scope", "global", "--scope", "global"],
            "DuplicateFlag",
        ),
        (vec!["recall", "--scope", "invalid"], "InvalidInput"),
        (vec!["recall", "--scope", "project"], "InvalidScope"),
    ] {
        refused(&args, b"", reason);
    }
    refused(
        &["remember", "--scope", "global"],
        b"{bad",
        "InvalidEnvelope",
    );
}
#[test]
fn binary_scope_binding_and_routing_reasons() {
    refused(
        &["remember", "--scope", "global"],
        &fact("plangraph"),
        "InvalidScope",
    );
    for op in ["remember", "recall"] {
        refused(
            &[op, "--scope", "project", "--project", "routing-private"],
            &fact("routing-private"),
            "InvalidScope",
        );
    }
}
#[test]
fn binary_configured_transport_outage_refuses() {
    refused(&["recall", "--scope", "global"], b"", "Outage");
    // Configured transport cannot save through an unavailable endpoint.
    refused(
        &["remember", "--scope", "global"],
        &fact("global"),
        "Outage",
    );
}
#[test]
fn binary_remember_caps_input_without_echoing_payload() {
    refused(
        &["remember", "--scope", "global"],
        &vec![b'x'; 1024 * 1024 + 1],
        "InputTooLarge",
    );
}
fn replies(body: &[u8]) -> Vec<Value> {
    let out = run(&["mcp"], body);
    assert_eq!(out.status.code(), Some(0));
    assert!(out.stderr.is_empty());
    String::from_utf8(out.stdout)
        .unwrap()
        .lines()
        .map(|line| serde_json::from_str(line).unwrap())
        .collect()
}
#[test]
fn malformed_mcp_line_does_not_kill_server() {
    let out = replies(b"{bad\n{\"id\":2,\"method\":\"initialize\"}\n");
    assert_eq!(out.len(), 2);
    assert_eq!(out[0]["error"]["code"], -32700);
    assert_eq!(out[0]["id"], Value::Null);
    assert_eq!(out[1]["id"], 2);
    assert_eq!(out[1]["result"]["serverInfo"]["name"], "og-memory");
}
#[test]
fn oversized_mcp_line_is_drained_and_next_request_survives() {
    let mut body = vec![b'x'; 1024 * 1024 + 20];
    body.extend_from_slice(b"\n{\"id\":3,\"method\":\"initialize\"}\n");
    let out = replies(&body);
    assert_eq!(out.len(), 2);
    assert_eq!(out[0]["error"]["message"], "InputTooLarge");
    assert_eq!(out[1]["id"], 3);
}
