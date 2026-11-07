use super::*;
use crate::io::Transport;
use std::{
    io::{Read, Write},
    net::TcpListener,
    os::unix::fs::OpenOptionsExt,
    thread,
    time::Duration,
};

fn publish(updated: bool, fault: &str) -> Result<Receipt, Error> {
    let listener = TcpListener::bind((std::net::Ipv4Addr::LOCALHOST, 0)).unwrap();
    let path = std::env::temp_dir().join(format!(
        "og-publish-{}-{:?}",
        std::process::id(),
        thread::current().id()
    ));
    let mut file = std::fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(&path)
        .unwrap();
    write!(file, "local-{}", std::process::id()).unwrap();
    let mut client = Http::new(
        &format!("http://{}/graphs/local", listener.local_addr().unwrap()),
        &path,
        Duration::from_millis(100),
    )
    .unwrap();
    std::fs::remove_file(path).unwrap();
    let fault = fault.to_owned();
    let server = thread::spawn(move || {
        let mut record = Value::Null;
        let mut state = Value::Null;
        for step in 0..5 {
            let (mut stream, _) = listener.accept().unwrap();
            stream
                .set_read_timeout(Some(Duration::from_secs(2)))
                .unwrap();
            let mut bytes = Vec::new();
            while !bytes.ends_with(b"\r\n\r\n") {
                let mut byte = [0];
                stream.read_exact(&mut byte).unwrap();
                bytes.push(byte[0]);
            }
            let headers = String::from_utf8(bytes).unwrap().to_lowercase();
            let length = headers
                .lines()
                .find_map(|s| s.strip_prefix("content-length: "))
                .unwrap()
                .parse()
                .unwrap();
            let mut body = vec![0; length];
            stream.read_exact(&mut body).unwrap();
            let request: Value = serde_json::from_slice(&body).unwrap();
            let commit = |id| {
                json!({"commit":{"graph_commit_id":id,"created_at":1,
                "graph_manifest_version":1,"graph_branch":"local"}})
            };
            if step == 0 && fault == "conflict" {
                write!(stream, "HTTP/1.1 412 Precondition Failed\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{{}}").unwrap();
                return;
            }
            if step == 0 && fault == "timeout" {
                thread::sleep(Duration::from_millis(200));
                return;
            }
            let response = match step {
                0 => {
                    assert!(headers.contains("omnigraph-if-graph-commit: before\r\n"));
                    assert!(request["query"]
                        .as_str()
                        .unwrap()
                        .contains("insert MemoryRecord"));
                    record = request["params"].clone();
                    assert_eq!(
                        record["content"],
                        if updated {
                            "Changed choice"
                        } else {
                            "Use tests"
                        }
                    );
                    commit("publication")
                }
                1 => {
                    assert_eq!(request["snapshot"], "publication");
                    json!({"graph_commit_id":"publication","row_count":1,"rows":[record]})
                }
                2 => {
                    assert!(headers.contains("omnigraph-if-graph-commit: publication\r\n"));
                    assert!(request["query"]
                        .as_str()
                        .unwrap()
                        .contains("insert MemorySavedState"));
                    state = request["params"].clone();
                    assert_eq!(
                        state["receiptJson"],
                        commit("publication")["commit"].to_string()
                    );
                    assert_eq!(state["superseded"], false);
                    commit("followup")
                }
                _ => {
                    assert_eq!(request["snapshot"], "followup");
                    json!({"graph_commit_id":"followup","row_count":1,
                        "rows":[if step == 3 {&record} else {&state}]})
                }
            }
            .to_string();
            write!(
                stream,
                "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                response.len(),
                response
            )
            .unwrap();
        }
    });
    let mut e = crate::tests::fact();
    e.content = if updated {
        "Changed choice"
    } else {
        "Use tests"
    }
    .into();
    let caps =
        crate::Capabilities::from_schema(include_str!("../tests/fixtures/memory-v1-approved.pg"));
    let result = client.commit(&crate::validate(&e, &caps).unwrap(), "before");
    server.join().unwrap();
    result
}

#[test]
fn remember_upserts_then_stores_real_receipt_and_verifies_followup() {
    assert!(publish(false, "").is_ok());
}
#[test]
fn update_upserts_with_if_graph_commit() {
    assert!(publish(true, "").is_ok());
}
#[test]
fn publication_412_returns_conflict_without_replay() {
    assert_eq!(publish(false, "conflict"), Err(Error::Conflict));
}
#[test]
fn publication_timeout_is_outcome_unknown() {
    assert_eq!(publish(false, "timeout"), Err(Error::Unknown));
}
