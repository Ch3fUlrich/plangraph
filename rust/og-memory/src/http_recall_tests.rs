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
        for index in 0..3 {
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
            assert_eq!(
                request["params"],
                if index == 0 {
                    json!({})
                } else {
                    json!({"project":"global"})
                }
            );
            assert!(request.get("branch").is_none());
            let mut row = if index == 0 {
                assert!(request["snapshot"].is_null());
                assert!(request["query"]
                    .as_str()
                    .unwrap()
                    .contains("memorySnapshot"));
                json!({"slug":"global"})
            } else if index == 1 {
                assert_eq!(request["snapshot"], "before");
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
            if index == 1 && fault == "metadata" {
                row["actor"] = Value::Null;
            }
            if index == 2 && fault == "key" {
                row["project"] = json!("other");
            }
            let rows = if index == 2 && fault == "missing" {
                vec![]
            } else if index == 2 && fault == "extra" {
                let mut extra = row.clone();
                extra["slug"] = json!("extra-state");
                vec![row, extra]
            } else {
                vec![row]
            };
            let mut response = json!({"graph_commit_id":if (index == 2 && fault == "commit") || (index == 1 && fault == "records-pin") {"different"} else {"before"},
                "row_count":if index == 2 && fault == "count" {2} else {rows.len()}, "rows":rows});
            if (index == 2 && fault == "unpinned") || (index == 0 && fault == "resolve-unpinned") {
                response.as_object_mut().unwrap().remove("graph_commit_id");
            }
            if (index == 2 && fault == "empty-pin") || (index == 0 && fault == "resolve-empty-pin")
            {
                response["graph_commit_id"] = json!("");
            }
            let response = response.to_string();
            write!(
                stream,
                "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                response.len(),
                response
            )
            .unwrap();
            if (index == 1 && fault == "records-pin")
                || (index == 0
                    && matches!(fault.as_str(), "resolve-unpinned" | "resolve-empty-pin"))
            {
                listener.set_nonblocking(true).unwrap();
                return listener;
            }
        }
        listener
    });
    let result = client.read("global");
    let listener = server.join().unwrap();
    listener.set_nonblocking(true).unwrap();
    assert_eq!(
        listener.accept().unwrap_err().kind(),
        std::io::ErrorKind::WouldBlock
    );
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
    for fault in [
        "metadata",
        "key",
        "missing",
        "commit",
        "count",
        "unpinned",
        "empty-pin",
        "extra",
        "records-pin",
    ] {
        assert!(matches!(recall(fault), Err(Error::Unverified)));
    }
}

#[test]
fn recall_rejects_missing_and_empty_resolve_pins_before_companion_queries() {
    for fault in ["resolve-unpinned", "resolve-empty-pin"] {
        assert!(matches!(recall(fault), Err(Error::Unverified)));
    }
}

#[test]
fn unavailable_publication_is_terminal_without_readback_or_retry() {
    struct Refusing {
        reads: usize,
        writes: usize,
    }
    impl Transport for Refusing {
        fn read(&mut self, _: &str) -> Result<crate::io::Snapshot, Error> {
            self.reads += 1;
            Ok(crate::io::Snapshot {
                graph_commit: "before".into(),
                facts: vec![],
            })
        }
        fn commit(&mut self, _: &crate::Validated, _: &str) -> Result<crate::io::Receipt, Error> {
            self.writes += 1;
            Err(Error::Unavailable)
        }
    }
    let mut memory = crate::io::Memory::new(Refusing {
        reads: 0,
        writes: 0,
    });
    let caps =
        crate::Capabilities::from_schema(include_str!("../tests/fixtures/memory-v1-approved.pg"));
    assert_eq!(
        memory.remember("global", &crate::tests::fact(), &caps),
        Err(Error::Unavailable)
    );
    assert_eq!((memory.transport.reads, memory.transport.writes), (1, 1));
    assert_eq!(crate::adapters::exit_code(&Err("Unavailable".into())), 1);
}
