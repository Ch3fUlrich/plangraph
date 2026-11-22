use std::io::{BufRead, BufReader, Write};
use std::net::TcpListener;
use std::process::Command;
use std::time::Duration;

#[test]
fn keeper_events_reads_a_page_using_the_principal_token() {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    listener.set_nonblocking(true).unwrap();
    let address = listener.local_addr().unwrap();
    let server = std::thread::spawn(move || {
        let deadline = std::time::Instant::now() + Duration::from_secs(5);
        let mut stream = loop {
            match listener.accept() {
                Ok((stream, _)) => break stream,
                Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                    assert!(std::time::Instant::now() < deadline, "no keeper request");
                    std::thread::sleep(Duration::from_millis(10));
                }
                Err(e) => panic!("{e}"),
            }
        };
        stream
            .set_read_timeout(Some(Duration::from_secs(2)))
            .unwrap();
        let lines: Vec<_> = BufReader::new(&stream)
            .lines()
            .map(Result::unwrap)
            .take_while(|line| !line.is_empty())
            .collect();
        assert_eq!(lines[0], "GET /v1/events?since=7&limit=10 HTTP/1.1");
        assert!(lines
            .iter()
            .any(|line| line.eq_ignore_ascii_case("Authorization: Bearer fixture-token")));
        let body = r#"{"rows":[],"next_cursor":7,"more":false}"#;
        write!(
            stream,
            "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\n\r\n{body}",
            body.len()
        )
        .unwrap();
    });
    let dir = tempfile::tempdir().unwrap();
    let token = dir.path().join("token");
    std::fs::write(&token, "fixture-token\n").unwrap();
    let output = Command::new(env!("CARGO_BIN_EXE_plangraph"))
        .env("PLANGRAPH_ENV_FILE", dir.path().join("env"))
        .env("PG_URL", format!("http://{address}"))
        .env("PG_TOKEN_FILE", token)
        .args(["keeper", "events", "--since", "7", "--limit", "10"])
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    server.join().unwrap();
    let page: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(page["next_cursor"], 7);
}
