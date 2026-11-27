use super::super::tests::{REQUEST_TIMEOUT, WATCHDOG};
use super::*;
use crate::io::Transport;
use std::{
    io::{ErrorKind::WouldBlock, Read, Write},
    net::TcpListener,
    os::unix::fs::OpenOptionsExt,
    sync::mpsc::{self, Receiver, RecvTimeoutError},
    thread,
    time::{Duration, Instant},
};

pub(in crate::http) struct Exchanges {
    listener: TcpListener,
    complete: Receiver<()>,
}
impl Exchanges {
    pub(in crate::http) fn accept(&self) -> std::net::TcpStream {
        let deadline = Instant::now() + WATCHDOG;
        loop {
            match self.listener.accept() {
                Ok((stream, _)) => {
                    stream.set_read_timeout(Some(WATCHDOG)).unwrap();
                    stream.set_write_timeout(Some(WATCHDOG)).unwrap();
                    return stream;
                }
                Err(e) if e.kind() == WouldBlock => {
                    assert!(Instant::now() < deadline, "missing fixture request");
                    let complete = self.complete.recv_timeout(Duration::from_millis(5));
                    assert_eq!(complete, Err(RecvTimeoutError::Timeout));
                }
                Err(e) => panic!("fixture accept: {e}"),
            }
        }
    }
    pub(in crate::http) fn finish(&self) {
        self.complete.recv_timeout(WATCHDOG).unwrap();
        assert_eq!(self.listener.accept().unwrap_err().kind(), WouldBlock);
    }
}
impl Http {
    #[rustfmt::skip]
    pub(in crate::http) fn fixture_exchanges(listener: TcpListener, complete: Receiver<()>) -> Exchanges {
        listener.set_nonblocking(true).unwrap();
        Exchanges { listener, complete }
    }
}

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
        if fault.ends_with("timeout") {
            REQUEST_TIMEOUT
        } else {
            WATCHDOG
        },
    )
    .unwrap();
    std::fs::remove_file(path).unwrap();
    let fault = fault.to_owned();
    let (done, complete) = mpsc::channel();
    let server = thread::spawn(move || {
        let fixture = Http::fixture_exchanges(listener, complete);
        let mut record = Value::Null;
        let mut state = Value::Null;
        let requests = match fault.as_str() {
            "conflict" | "timeout" => 1,
            "changed-value" => 2,
            "state-conflict" | "state-timeout" => 3,
            "snapshot" => 4,
            _ => 5,
        };
        for step in 0..requests {
            let mut stream = fixture.accept();
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
            let mut response = match step {
                0 => {
                    let expected = if updated { "updated-base" } else { "before" };
                    assert!(headers.contains(&format!("omnigraph-if-graph-commit: {expected}\r\n")));
                    assert!(request["query"]
                        .as_str()
                        .unwrap()
                        .contains("insert MemoryRecord"));
                    record = request["params"].clone();
                    if fault == "extra-column" {
                        record["serverColumn"] = json!(42);
                    }
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
                    if fault == "changed-value" {
                        record["version"] = json!("1");
                    }
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
            };
            if (step == 0 && fault == "conflict") || (step == 2 && fault == "state-conflict") {
                write!(stream, "HTTP/1.1 412 Precondition Failed\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{{}}").unwrap();
                break;
            }
            if (step == 0 && fault == "timeout") || (step == 2 && fault == "state-timeout") {
                fixture.finish();
                return;
            }
            if step == 3 && fault == "snapshot" {
                response["graph_commit_id"] = json!("different");
            }
            if step == 4 && fault == "receipt" {
                response["rows"][0]["receiptJson"] =
                    commit("different")["commit"].to_string().into();
            }
            let response = response.to_string();
            write!(
                stream,
                "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                response.len(),
                response
            )
            .unwrap();
        }
        fixture.finish();
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
    let result = client.commit(
        &crate::validate(&e, &caps).unwrap(),
        if updated { "updated-base" } else { "before" },
    );
    let _ = done.send(());
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
fn extra_live_column_is_ignored() {
    assert!(publish(false, "extra-column").is_ok());
}
#[test]
fn changed_written_value_is_refused() {
    partial("changed-value", Error::Unverified);
}
#[test]
fn publication_412_returns_conflict_without_replay() {
    assert_eq!(publish(false, "conflict"), Err(Error::Conflict));
}
#[test]
fn publication_timeout_is_outcome_unknown() {
    assert_eq!(publish(false, "timeout"), Err(Error::Unknown));
}

fn partial(fault: &str, cause: Error) {
    assert_eq!(
        publish(false, fault),
        Err(Error::PartialPublication {
            commit: "publication".into(),
            cause: Box::new(cause),
        })
    );
}
#[test]
fn state_followup_412_is_partial_publication_without_replay() {
    partial("state-conflict", Error::Conflict);
}
#[test]
fn state_followup_timeout_is_partial_publication_outcome_unknown() {
    partial("state-timeout", Error::Unknown);
}
#[test]
fn mismatched_read_at_snapshot_is_partial_publication() {
    partial("snapshot", Error::Unverified);
}
#[test]
fn receipt_mismatch_is_partial_publication() {
    partial("receipt", Error::Unverified);
}

#[test]
fn memory_surfaces_partial_publication_without_retry_or_readback() {
    struct PartialTransport(usize);
    impl Transport for PartialTransport {
        fn read(&mut self, project: &str) -> Result<crate::io::Snapshot, Error> {
            assert_eq!(self.0, 0, "no readback after partial publication");
            self.0 += 1;
            Ok(crate::io::Snapshot {
                graph_commit: project.into(),
                facts: vec![],
            })
        }
        fn commit(&mut self, _: &Validated, _: &str) -> Result<Receipt, Error> {
            assert_eq!(self.0, 1, "no replay after partial publication");
            self.0 += 1;
            Err(Error::PartialPublication {
                commit: "publication".into(),
                cause: Box::new(Error::Conflict),
            })
        }
    }
    let mut memory = crate::io::Memory::new(PartialTransport(0));
    let caps = crate::Capabilities::from_schema(include_str!("../tests/fixtures/memory.pg"));
    let result = memory.remember("global", &crate::tests::fact(), &caps);
    assert_eq!(
        result,
        Err(Error::PartialPublication {
            commit: "publication".into(),
            cause: Box::new(Error::Conflict),
        })
    );
    assert_eq!(memory.transport.0, 2);
}
