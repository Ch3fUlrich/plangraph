#![cfg(unix)]
use std::{
    io::{Read, Write},
    net::TcpListener,
    os::unix::fs::OpenOptionsExt,
    process::Command,
    thread,
};

fn command() -> Command {
    let mut command = Command::new(env!("CARGO_BIN_EXE_og-memory"));
    command
        .env_remove("OG_MEMORY_BASE_URL")
        .env_remove("OG_MEMORY_TOKEN_FILE");
    command.args(["recall", "--scope", "global"]);
    command
}

#[test]
fn unconfigured_binary_refuses() {
    let output = command().output().unwrap();
    assert!(!output.status.success());
    assert_eq!(output.stderr, b"HttpUnconfigured\n");
}

#[test]
fn unreadable_token_refuses_without_exposing_path() {
    let output = command()
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
        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(15);
        for step in 0..3 {
            let mut stream = loop {
                match listener.accept() {
                    Ok((stream, _)) => break stream,
                    Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                        assert!(std::time::Instant::now() < deadline, "missing HTTP request");
                        thread::sleep(std::time::Duration::from_millis(5));
                    }
                    Err(_) => panic!("fake server accept failed"),
                }
            };
            stream
                .set_read_timeout(Some(std::time::Duration::from_secs(5)))
                .unwrap();
            let mut headers = Vec::new();
            while !headers.ends_with(b"\r\n\r\n") {
                let mut byte = [0];
                stream.read_exact(&mut byte).unwrap();
                headers.push(byte[0]);
            }
            let headers = String::from_utf8(headers).unwrap().to_lowercase();
            assert!(headers.starts_with("post /graphs/local/query "));
            assert!(headers.contains(&format!("authorization: bearer {token}\r\n")));
            let length: usize = headers
                .lines()
                .find_map(|s| s.strip_prefix("content-length: "))
                .unwrap()
                .parse()
                .unwrap();
            let mut body = vec![0; length];
            stream.read_exact(&mut body).unwrap();
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
    let output = command()
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
