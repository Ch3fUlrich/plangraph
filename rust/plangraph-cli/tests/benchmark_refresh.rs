use std::io::{BufRead, BufReader, Write};
use std::net::{TcpListener, TcpStream};
use std::path::Path;
use std::process::Command;
use std::thread;
use std::time::Duration;

fn request(stream: &mut TcpStream) -> String {
    stream
        .set_read_timeout(Some(Duration::from_secs(3)))
        .unwrap();
    let mut reader = BufReader::new(stream);
    let mut headers = String::new();
    loop {
        let mut line = String::new();
        reader.read_line(&mut line).unwrap();
        if line == "\r\n" || line.is_empty() {
            return headers;
        }
        headers.push_str(&line);
    }
}

#[test]
fn refresh_reuses_both_validators_and_writes_no_rows_on_304() {
    let directory = tempfile::tempdir().unwrap();
    let db = directory.path().join("bench.sqlite");
    let repo = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let archive = Command::new("python3")
        .args(["-c", r#"
import io, sys, zipfile
data = io.BytesIO()
with zipfile.ZipFile(data, 'w') as archive:
    archive.writestr('cursorbench_external.csv',
        'Model version,Name,Reasoning level,Score,Cost per task,Source,Date of evaluation\n' +
        ''.join(f'claude-fable-5-2_{level},Claude Fable 5.2 ({level}),{level},0.55,,https://example.invalid/bench,2026-11-20\n'
                for level in ('low','medium','high','xhigh','max')))
    archive.writestr('aider_polyglot_external.csv', 'Model version,Score\nforbidden_high,100\n')
sys.stdout.buffer.write(data.getvalue())
"#])
        .output()
        .unwrap();
    assert!(archive.status.success());
    let server = TcpListener::bind("127.0.0.1:0").unwrap();
    let endpoint = format!("http://{}", server.local_addr().unwrap());
    let worker = thread::spawn(move || {
        for round in 0..2 {
            for _ in 0..2 {
                let mut stream = server.accept().unwrap().0;
                let headers = request(&mut stream).to_ascii_lowercase();
                let body = if headers.starts_with("get /epoch ") {
                    &archive.stdout[..]
                } else {
                    assert!(headers.starts_with("get /models "), "{headers}");
                    br#"{"data":[{"id":"anthropic/claude-fable-5.2","reasoning":{"default_effort":"high"},"benchmarks":{"artificial_analysis":{"intelligence_index":64.6}},"pricing":{"prompt":"0.000008","completion":"0.00004"}}]}"#
                };
                let status = if round == 0 {
                    assert!(!headers.contains("if-none-match:"));
                    "200 OK"
                } else {
                    assert!(headers.contains("if-none-match: \"sample\""), "{headers}");
                    assert!(
                        headers.contains("if-modified-since: fri, 20 nov 2026 00:00:00 gmt"),
                        "{headers}"
                    );
                    "304 Not Modified"
                };
                let body = if round == 0 { body } else { &[] };
                write!(stream, "HTTP/1.1 {status}\r\nETag: \"sample\"\r\nLast-Modified: Fri, 20 Nov 2026 00:00:00 GMT\r\nContent-Length: {}\r\nConnection: close\r\n\r\n", body.len()).unwrap();
                stream.write_all(body).unwrap();
            }
        }
    });
    let refresh = || {
        let output = Command::new("python3")
            .current_dir(&repo)
            .env(
                "PLANGRAPH_BENCH_REFRESH_BIN",
                env!("CARGO_BIN_EXE_plangraph"),
            )
            .args(["tools/bench_gate.py", "--refresh", "--db"])
            .arg(&db)
            .args([
                "--epoch-url",
                &format!("{endpoint}/epoch"),
                "--openrouter-url",
                &format!("{endpoint}/models"),
            ])
            .output()
            .unwrap();
        assert!(
            output.status.success(),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
        serde_json::from_slice::<serde_json::Value>(&output.stdout).unwrap()
    };
    assert_eq!(refresh()["rows_written"], 6);
    let catalog = rusqlite::Connection::open(&db).unwrap();
    let (rows, null_costs): (u32, u32) = catalog.query_row(
        "SELECT count(*), count(*)-count(cost_per_task_usd) FROM catalog_benchmark_rows
         WHERE model_id='claude-fable-5-2' AND measured_at='2026-11-20' AND effort_channel='publisher'",
        [], |r| Ok((r.get(0)?, r.get(1)?))).unwrap();
    assert_eq!((rows, null_costs), (5, 5));
    let effort: String = catalog
        .query_row(
            "SELECT effort FROM catalog_benchmark_rows WHERE model_id='anthropic/claude-fable-5.2'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert!(effort.is_empty());
    let attribution: String = catalog
        .query_row(
            "SELECT attribution FROM catalog_benchmark_refresh_sources WHERE source='epoch'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert!(attribution.contains("Epoch AI") && attribution.contains("CC BY 4.0"));
    drop(catalog);
    let before = std::fs::read(&db).unwrap();
    assert_eq!(refresh()["rows_written"], 0);
    assert_eq!(std::fs::read(&db).unwrap(), before);
    worker.join().unwrap();
}
