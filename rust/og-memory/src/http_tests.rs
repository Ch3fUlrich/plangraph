use super::*;
use std::{
    io::{Read, Write},
    net::TcpListener,
    os::unix::fs::OpenOptionsExt,
    thread,
};

#[test]
fn loopback_validation_accepts_only_local_hosts() {
    assert!(loopback("localhost"));
    assert!(loopback("LOCALHOST"));
    for address in [
        std::net::IpAddr::V4(std::net::Ipv4Addr::LOCALHOST),
        std::net::IpAddr::V6(std::net::Ipv6Addr::LOCALHOST),
    ] {
        assert!(loopback(&address.to_string()));
    }
    assert!(loopback(&format!("[{}]", std::net::Ipv6Addr::LOCALHOST)));
    assert!(!loopback("remote.invalid"));
    assert!(!loopback(&std::net::Ipv4Addr::UNSPECIFIED.to_string()));
}

fn exercise(status: &str, body: &str, delay: bool, write: bool) -> Result<Value, Error> {
    let listener = TcpListener::bind((std::net::Ipv4Addr::LOCALHOST, 0)).unwrap();
    let base = format!("http://{}/graphs/local", listener.local_addr().unwrap());
    let path = std::env::temp_dir().join(format!(
        "og-http-{}-{:?}",
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
    let client = Http::new(&base, &path, Duration::from_millis(100)).unwrap();
    assert!(client.agent.config().proxy().is_none());
    std::fs::remove_file(path).unwrap();
    let status = status.to_owned();
    let body = body.to_owned();
    let server = thread::spawn(move || {
        let (mut stream, _) = listener.accept().unwrap();
        stream
            .set_read_timeout(Some(Duration::from_secs(2)))
            .unwrap();
        let mut bytes = Vec::new();
        let header_end = loop {
            let mut byte = [0];
            stream.read_exact(&mut byte).unwrap();
            bytes.push(byte[0]);
            if bytes.ends_with(b"\r\n\r\n") {
                break bytes.len();
            }
        };
        let headers = String::from_utf8(bytes[..header_end].to_vec())
            .unwrap()
            .to_lowercase();
        assert!(headers.contains("omnigraph-http-api: 0.13\r\n"));
        assert!(headers.contains(&format!(
            "authorization: bearer local-{}\r\n",
            std::process::id()
        )));
        let length: usize = headers
            .lines()
            .find_map(|l| l.strip_prefix("content-length: "))
            .unwrap()
            .parse()
            .unwrap();
        let mut payload = vec![0; length];
        stream.read_exact(&mut payload).unwrap();
        let payload: Value = serde_json::from_slice(&payload).unwrap();
        assert_eq!(payload["params"]["project"], "global");
        assert_eq!(payload["params"]["count"], 1);
        if write {
            assert!(headers.starts_with("post /graphs/local/mutate/if-graph-commit "));
            assert!(headers.contains("omnigraph-if-graph-commit: before\r\n"));
        } else {
            assert!(headers.starts_with("post /graphs/local/query "));
        }
        if delay {
            thread::sleep(Duration::from_millis(200));
        }
        if !status.is_empty() {
            let _ = write!(
                stream,
                "HTTP/1.1 {status}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                body.len()
            );
        }
        drop(stream);
        // Watch beyond the client's entire timeout budget, not just response dispatch.
        thread::sleep(Duration::from_millis(150));
        listener.set_nonblocking(true).unwrap();
        assert_eq!(
            listener.accept().unwrap_err().kind(),
            std::io::ErrorKind::WouldBlock,
            "must not retry"
        );
    });
    #[derive(Serialize)]
    struct Params {
        project: &'static str,
        count: u32,
    }
    let request = Request {
        query: "local fake only",
        params: Params {
            project: "global",
            count: 1,
        },
    };
    let result = if write {
        client.mutate(&request, "before")
    } else {
        client.query(&request)
    };
    server.join().unwrap();
    result
}

#[test]
fn conditional_412_is_conflict_without_retry() {
    assert!(matches!(
        exercise("412 Precondition Failed", "{}", false, true),
        Err(Error::Conflict)
    ));
}
#[test]
fn timeout_is_unknown_without_retry() {
    assert!(matches!(
        exercise("200 OK", "{}", true, true),
        Err(Error::Unknown)
    ));
}
#[test]
fn success_preserves_publication_receipt() {
    let result = exercise(
        "200 OK",
        r#"{"commit":{"graphCommitId":"after"}}"#,
        false,
        true,
    )
    .unwrap();
    assert_eq!(result["commit"]["graphCommitId"], "after");
}
#[test]
fn query_sends_typed_params_and_bearer_header() {
    assert!(exercise(
        "200 OK",
        r#"{"graphCommitId":"before","results":[]}"#,
        false,
        false
    )
    .is_ok());
}
#[test]
fn malformed_write_response_is_unknown() {
    assert!(matches!(
        exercise("200 OK", "invalid", false, true),
        Err(Error::Unknown)
    ));
}

#[test]
fn lost_write_response_is_unknown_without_retry() {
    assert!(matches!(exercise("", "", false, true), Err(Error::Unknown)));
}

fn assert_constructor_rejected(base: &str, timeout: Duration) {
    // A token-loading failure is also Outage: provide a valid file to isolate validation.
    let path = std::env::temp_dir().join(format!(
        "og-http-reject-{}-{:?}",
        std::process::id(),
        thread::current().id()
    ));
    let mut file = std::fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(&path)
        .unwrap();
    file.write_all(b"local-test").unwrap();
    let result = Http::new(base, &path, timeout);
    std::fs::remove_file(path).unwrap();
    assert!(matches!(result, Err(Error::Outage)));
}

#[test]
fn constructor_rejects_non_loopback_http() {
    assert_constructor_rejected("http://remote.invalid/graphs/local", Duration::from_secs(1));
}

#[test]
fn constructor_rejects_userinfo() {
    assert_constructor_rejected(
        "https://user@remote.invalid/graphs/local",
        Duration::from_secs(1),
    );
}

#[test]
fn constructor_rejects_zero_timeout() {
    assert_constructor_rejected("https://remote.invalid/graphs/local", Duration::ZERO);
}

#[test]
fn malformed_status_is_unknown_without_retry() {
    assert!(matches!(
        exercise("invalid", "", false, true),
        Err(Error::Unknown)
    ));
}

#[test]
fn non_success_statuses_never_succeed() {
    for status in ["302 Found", "401 Unauthorized", "500 Internal Server Error"] {
        assert!(matches!(
            exercise(status, "{}", false, true),
            Err(Error::Unknown)
        ));
        assert!(matches!(
            exercise(status, "{}", false, false),
            Err(Error::Outage)
        ));
    }
}

#[test]
fn access_checks_health_and_authenticated_project_hub() {
    const OK: &str = r#"{"graph_commit_id":"fixture","row_count":1,"rows":[{"slug":"plangraph"}]}"#;
    const EMPTY: &str = r#"{"graph_commit_id":"fixture","row_count":0,"rows":[]}"#;
    for (graph, reply, expected) in [
        ("plangraph", Some(("200 OK", OK)), Ok(())),
        ("another", None, Err(AccessError::WrongGraph)),
        (
            "plangraph",
            Some(("200 OK", EMPTY)),
            Err(AccessError::MissingHub),
        ),
        (
            "plangraph",
            Some(("401 Unauthorized", "")),
            Err(AccessError::Unauthorized),
        ),
        (
            "plangraph",
            Some(("403 Forbidden", "")),
            Err(AccessError::Forbidden),
        ),
        (
            "plangraph",
            Some(("426 Upgrade Required", "")),
            Err(AccessError::UnsupportedApi),
        ),
        (
            "plangraph",
            Some(("200 OK", "{}")),
            Err(AccessError::UnsupportedApi),
        ),
        ("plangraph", Some(("", "")), Err(AccessError::ReadTimeout)),
    ] {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let base = format!("http://{}/graphs/{graph}", listener.local_addr().unwrap());
        let path = std::env::temp_dir().join(format!("og-access-{}", std::process::id()));
        let mut file = std::fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(&path)
            .unwrap();
        write!(file, "local-{}", std::process::id()).unwrap();
        let client = Http::new(&base, &path, Duration::from_millis(300)).unwrap();
        std::fs::remove_file(path).unwrap();
        let server = thread::spawn(move || {
            let (mut health, _) = listener.accept().unwrap();
            let headers = access_headers(&mut health);
            assert!(headers.starts_with("get /healthz "));
            assert!(!headers.contains("authorization:"));
            health
                .write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                .unwrap();
            drop(health);
            if let Some((status, body)) = reply {
                listener.set_nonblocking(true).unwrap();
                let deadline = std::time::Instant::now() + Duration::from_secs(2);
                let mut query = loop {
                    match listener.accept() {
                        Ok((stream, _)) => break stream,
                        Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                            assert!(std::time::Instant::now() < deadline, "missing hub request");
                            thread::sleep(Duration::from_millis(5));
                        }
                        Err(_) => panic!("fake server accept failed"),
                    }
                };
                let headers = access_headers(&mut query);
                assert!(headers.starts_with("post /graphs/plangraph/query "));
                assert!(headers.contains("authorization: bearer "));
                assert!(headers.contains("omnigraph-http-api: 0.13"));
                let length = headers
                    .lines()
                    .find_map(|line| line.strip_prefix("content-length: "))
                    .unwrap()
                    .parse()
                    .unwrap();
                let mut payload = vec![0; length];
                query.read_exact(&mut payload).unwrap();
                let request: Value = serde_json::from_slice(&payload).unwrap();
                assert_eq!(request["params"]["slug"], "plangraph");
                if status.is_empty() {
                    thread::sleep(Duration::from_millis(450));
                } else {
                    write!(query, "HTTP/1.1 {status}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}", body.len()).unwrap();
                }
            }
        });
        assert_eq!(client.probe_access(), expected);
        server.join().unwrap();
    }
}

fn access_headers(stream: &mut std::net::TcpStream) -> String {
    stream
        .set_read_timeout(Some(Duration::from_secs(2)))
        .unwrap();
    let mut headers = Vec::new();
    while !headers.ends_with(b"\r\n\r\n") {
        let mut byte = [0];
        stream.read_exact(&mut byte).unwrap();
        headers.push(byte[0]);
    }
    String::from_utf8(headers).unwrap().to_ascii_lowercase()
}
