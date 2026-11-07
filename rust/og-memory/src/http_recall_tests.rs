use super::*;
use crate::io::Transport;
use serde_json::json;
use std::{
    io::{Read, Write},
    net::TcpListener,
    os::unix::fs::OpenOptionsExt,
    thread,
};

fn recall(fault: &str) -> Result<crate::io::Snapshot, Error> {
    let listener = TcpListener::bind((std::net::Ipv4Addr::LOCALHOST, 0)).unwrap();
    let base = format!("http://{}/graphs/local", listener.local_addr().unwrap());
    let path = std::env::temp_dir().join(format!(
        "og-recall-{}-{:?}",
        std::process::id(),
        thread::current().id()
    ));
    let mut file = std::fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(&path)
        .unwrap();
    file.write_all(format!("local-{}", std::process::id()).as_bytes())
        .unwrap();
    let mut client = Http::new(&base, &path, Duration::from_secs(2)).unwrap();
    std::fs::remove_file(path).unwrap();
    let fault = fault.to_owned();
    let server = thread::spawn(move || {
        for index in 0..2 {
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
            assert!(headers.starts_with("post /graphs/local/query "));
            assert!(!headers.contains("omnigraph-if-graph-commit:"));
            let length = headers
                .lines()
                .find_map(|l| l.strip_prefix("content-length: "))
                .unwrap()
                .parse()
                .unwrap();
            let mut body = vec![0; length];
            stream.read_exact(&mut body).unwrap();
            let request: Value = serde_json::from_slice(&body).unwrap();
            assert_eq!(request["params"], json!({"project":"global"}));
            assert!(request.get("branch").is_none());
            let mut row = if index == 0 {
                assert!(request["snapshot"].is_null());
                assert!(request["query"].as_str().unwrap().contains("MemoryRecord"));
                json!({"project":"global","kind":"Decision","slug":"safe-choice","content":"Use tests",
                    "version":1,"sourceType":"file","sourcePath":"notes.md","sourceHash":"local",
                    "actor":"worker","relationsJson":"[]"})
            } else {
                assert_eq!(request["snapshot"], "before");
                assert!(request["query"]
                    .as_str()
                    .unwrap()
                    .contains("MemorySavedState"));
                json!({"project":"global","kind":"Decision","slug":"safe-choice",
                    "receiptJson":json!({"graph_commit_id":"publication","created_at":1,"graph_manifest_version":1}).to_string(),
                    "superseded":false})
            };
            if index == 0 && fault == "metadata" {
                row["actor"] = Value::Null;
            }
            if index == 1 && fault == "key" {
                row["project"] = json!("other");
            }
            let rows = if index == 1 && fault == "missing" {
                vec![]
            } else {
                vec![row]
            };
            let response = json!({"graph_commit_id":if index == 1 && fault == "commit" {"different"} else {"before"},
                "row_count":if index == 1 && fault == "count" {2} else {rows.len()}, "rows":rows}).to_string();
            write!(
                stream,
                "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                response.len(),
                response
            )
            .unwrap();
        }
    });
    let result = client.read("global");
    server.join().unwrap();
    result
}

#[test]
fn recall_joins_approved_companions_at_pinned_snapshot() {
    let snapshot = recall("").unwrap();
    assert_eq!(snapshot.graph_commit, "before");
    assert_eq!(snapshot.facts.len(), 1);
    assert_eq!(snapshot.facts[0].envelope.source.path, "notes.md");
    assert!(!snapshot.facts[0].superseded);
}

#[test]
fn recall_rejects_incomplete_mismatched_and_unpinned_companions() {
    for fault in ["metadata", "key", "missing", "commit", "count"] {
        assert!(matches!(recall(fault), Err(Error::Unverified)));
    }
}
