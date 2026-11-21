#![cfg(unix)]
use std::{
    io::{Read, Write},
    net::{TcpListener, TcpStream},
    os::unix::fs::OpenOptionsExt,
    process::Command,
    thread,
    time::{Duration, Instant},
};

fn binary(args: &[&str]) -> Command {
    let mut command = Command::new(env!("CARGO_BIN_EXE_og-memory"));
    command
        .env_remove("OG_MEMORY_BASE_URL")
        .env_remove("OG_MEMORY_TOKEN_FILE");
    command.args(args);
    command
}

#[test]
fn unconfigured_binary_refuses() {
    let output = binary(&["recall", "--scope", "global"]).output().unwrap();
    assert!(!output.status.success());
    assert_eq!(output.stderr, b"HttpUnconfigured\n");
}

#[test]
fn unreadable_token_refuses_without_exposing_path() {
    let output = binary(&["recall", "--scope", "global"])
        .env("OG_MEMORY_BASE_URL", "http://127.0.0.1:1/graphs/local")
        .env("OG_MEMORY_TOKEN_FILE", "/nonexistent/og-memory-token")
        .output()
        .unwrap();
    assert!(!output.status.success());
    assert_eq!(output.stderr, b"HttpConfigurationUnavailable\n");
}

#[test]
fn configured_binary_uses_pinned_http_recall() {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    listener.set_nonblocking(true).unwrap();
    let base = format!("http://{}/graphs/local", listener.local_addr().unwrap());
    let path = std::env::temp_dir().join(format!("og-binary-token-{}", std::process::id()));
    let token = format!("offline-{}", std::process::id());
    let mut file = std::fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(&path)
        .unwrap();
    write!(file, "{token}").unwrap();
    let server = thread::spawn(move || {
        for step in 0..3 {
            let (mut stream, headers, body) = receive_request(&listener);
            assert!(headers.starts_with("post /graphs/local/query "));
            assert!(headers.contains(&format!("authorization: bearer {token}\r\n")));
            let request: serde_json::Value = serde_json::from_slice(&body).unwrap();
            if step > 0 {
                assert_eq!(request["snapshot"], "offline-pin");
            }
            let response = if step == 0 {
                r#"{"graph_commit_id":"offline-pin"}"#
            } else {
                r#"{"graph_commit_id":"offline-pin","row_count":0,"rows":[]}"#
            };
            write!(
                stream,
                "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                response.len(),
                response
            )
            .unwrap();
        }
    });
    let output = binary(&["recall", "--scope", "global"])
        .env("OG_MEMORY_BASE_URL", base)
        .env("OG_MEMORY_TOKEN_FILE", &path)
        .output()
        .unwrap();
    std::fs::remove_file(path).unwrap();
    server.join().unwrap();
    assert!(output.status.success());
    assert!(output.stderr.is_empty());
    assert!(!String::from_utf8_lossy(&output.stdout).contains("offline-"));
}

#[test]
fn checks_retired_mcp_url_without_opening_transport() {
    let ipv6_loopback = format!("http://[{}]:8080", std::net::Ipv6Addr::LOCALHOST);
    for (base, expected) in [
        (None, 0),
        (Some("http://LOCALHOST:8080"), 1),
        (Some("http://127.0.0.1:8080/"), 1),
        (Some(ipv6_loopback.as_str()), 1),
        (Some("http://127.0.0.1:8081"), 0),
        (Some("https://local.example:8080"), 0),
    ] {
        let mut cmd = Command::new(env!("CARGO_BIN_EXE_og-memory"));
        cmd.arg("check-mcp-url")
            .env_remove("OG_MEMORY_BASE_URL")
            .env_remove("OG_MEMORY_TOKEN_FILE")
            .env_remove("OMNIGRAPH_BASE_URL");
        if let Some(base) = base {
            cmd.env("OMNIGRAPH_BASE_URL", base);
        }
        let result = cmd.output().unwrap();
        assert_eq!(result.status.code(), Some(expected));
        assert!(result.stdout.is_empty());
        if expected == 1 {
            assert_eq!(result.stderr, b"RetiredOmnigraphMcpUrl\n");
        } else {
            assert!(result.stderr.is_empty());
        }
    }
}

fn receive_request(listener: &TcpListener) -> (TcpStream, String, Vec<u8>) {
    let deadline = Instant::now() + Duration::from_secs(5);
    let mut stream = loop {
        match listener.accept() {
            Ok((stream, _)) => break stream,
            Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                assert!(Instant::now() < deadline, "missing HTTP request");
                thread::sleep(Duration::from_millis(5));
            }
            Err(_) => panic!("fake server accept failed"),
        }
    };
    stream
        .set_read_timeout(Some(Duration::from_secs(5)))
        .unwrap();
    let mut headers = Vec::new();
    while !headers.ends_with(b"\r\n\r\n") {
        let mut byte = [0];
        stream.read_exact(&mut byte).unwrap();
        headers.push(byte[0]);
    }
    let headers = String::from_utf8(headers).unwrap().to_ascii_lowercase();
    let length = headers
        .lines()
        .find_map(|line| line.strip_prefix("content-length: "))
        .map_or(0, |length| length.parse().unwrap());
    let mut body = vec![0; length];
    stream.read_exact(&mut body).unwrap();
    (stream, headers, body)
}

#[test]
fn unconfigured_access_probe_refuses() {
    let output = binary(&["probe-access"]).output().unwrap();
    assert_eq!(output.status.code(), Some(1));
    assert!(output.stdout.is_empty());
    assert_eq!(output.stderr, b"HttpUnconfigured\n");
}

#[test]
fn access_probe_binary_pins_its_output_contract() {
    let token = format!("fixture-{}", std::process::id());
    let path = std::env::temp_dir().join(format!("og-binary-access-{}", std::process::id()));
    let mut file = std::fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(&path)
        .unwrap();
    writeln!(file, "{token}").unwrap();
    for (status, body, expected_code, expected_stdout, expected_stderr) in [
        (
            "200 OK",
            r#"{"graph_commit_id":"fixture","row_count":1,"rows":[{"slug":"plangraph"}]}"#,
            0,
            "HealthOk AuthenticatedReadOk plangraph HTTP 0.13\n",
            "",
        ),
        ("401 Unauthorized", token.as_str(), 1, "", "Unauthorized\n"),
        (
            "426 Upgrade Required",
            token.as_str(),
            1,
            "",
            "UnsupportedApi\n",
        ),
    ] {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        listener.set_nonblocking(true).unwrap();
        let base = format!("http://{}/graphs/plangraph", listener.local_addr().unwrap());
        let private_value = token.clone();
        let response_body = body.to_owned();
        let server = thread::spawn(move || {
            let (mut health, headers, body) = receive_request(&listener);
            assert!(headers.starts_with("get /healthz "));
            assert!(!headers.contains("authorization:"));
            assert!(body.is_empty());
            health
                .write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                .unwrap();
            drop(health);

            let (mut query, headers, body) = receive_request(&listener);
            assert!(headers.starts_with("post /graphs/plangraph/query "));
            assert!(headers.contains(&format!("authorization: bearer {private_value}\r\n")));
            let request: serde_json::Value = serde_json::from_slice(&body).unwrap();
            assert_eq!(request["params"]["slug"], "plangraph");
            write!(query, "HTTP/1.1 {status}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{response_body}", response_body.len()).unwrap();
        });
        let output = binary(&["probe-access"])
            .env("OG_MEMORY_BASE_URL", base)
            .env("OG_MEMORY_TOKEN_FILE", &path)
            .output()
            .unwrap();
        server.join().unwrap();
        assert_eq!(output.status.code(), Some(expected_code));
        assert_eq!(output.stdout, expected_stdout.as_bytes());
        assert_eq!(output.stderr, expected_stderr.as_bytes());
    }
    std::fs::remove_file(path).unwrap();
}
