//! Run a command against an isolated Temporal dev server on Linux or Windows.
use std::io;
use std::net::TcpListener;
use std::process::{Child, Command, Stdio};
use std::time::{Duration, Instant};

struct Server(Child);
impl Drop for Server {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}

fn free_port() -> io::Result<u16> {
    Ok(TcpListener::bind((std::net::Ipv4Addr::LOCALHOST, 0))?
        .local_addr()?
        .port())
}

fn run() -> Result<i32, Box<dyn std::error::Error>> {
    let args: Vec<_> = std::env::args_os().skip(1).collect();
    if args.is_empty() {
        return Err("usage: ci-temporal COMMAND [ARG ...]".into());
    }
    let cli = std::env::var_os("TEMPORAL_CLI").unwrap_or_else(|| "temporal".into());
    let root = std::env::var_os("RUNNER_TEMP")
        .map(std::path::PathBuf::from)
        .unwrap_or_else(std::env::temp_dir);
    let mut builder = tempfile::Builder::new();
    builder.prefix("pg-temporal-");
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        builder.permissions(std::fs::Permissions::from_mode(0o700));
    }
    let temp = builder.tempdir_in(root)?;
    let log = temp.path().join("temporal.log");
    for attempt in 1..=3 {
        // The CLI cannot inherit the reserved listener. Retry a lost bind race.
        // Headless disables the UI; HTTP and metrics default to OS-picked ports.
        let address = format!("127.0.0.1:{}", free_port()?);
        let output = std::fs::File::create(&log)?;
        let mut server = Server(
            Command::new(&cli)
                .args([
                    "server",
                    "start-dev",
                    "--headless",
                    "--ip",
                    "127.0.0.1",
                    "--port",
                ])
                .arg(address.split(':').next_back().unwrap())
                .stdin(Stdio::null())
                .stderr(output.try_clone()?)
                .stdout(output)
                .spawn()?,
        );
        let deadline = Instant::now() + Duration::from_secs(60);
        let mut ready = false;
        while Instant::now() < deadline && server.0.try_wait()?.is_none() {
            if Command::new(&cli)
                .args([
                    "operator",
                    "cluster",
                    "health",
                    "--address",
                    &address,
                    "--command-timeout",
                    "2s",
                ])
                .stdin(Stdio::null())
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .status()?
                .success()
            {
                ready = true;
                break;
            }
            std::thread::sleep(Duration::from_millis(200));
        }
        let status = if ready {
            eprintln!("Temporal ready at {address}; log {}", log.display());
            Some(
                Command::new(&args[0])
                    .args(&args[1..])
                    .env("PG_TEMPORAL_ADDR", format!("http://{address}"))
                    .env("SPIKE_ADDR", format!("http://{address}"))
                    .env("TEMPORAL_TEST_ADDRESS", &address)
                    .env("TEMPORAL_ADDRESS", &address)
                    .status()?,
            )
        } else {
            None
        };
        drop(server);
        let contents = std::fs::read_to_string(&log)?;
        eprintln!(
            "Temporal attempt {attempt} log:\n{}",
            contents
                .lines()
                .rev()
                .take(100)
                .collect::<Vec<_>>()
                .into_iter()
                .rev()
                .collect::<Vec<_>>()
                .join("\n")
        );
        if let Some(status) = status {
            return Ok(status.code().unwrap_or(1));
        }
    }
    Err("Temporal failed to start after three attempts".into())
}

fn main() {
    match run() {
        Ok(code) => std::process::exit(code),
        Err(error) => {
            eprintln!("ci-temporal: {error}");
            std::process::exit(1);
        }
    }
}

#[cfg(test)]
mod tests {
    #[test]
    fn workflows_refuse_shared_temporal_endpoints_and_logs() {
        let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../../.github/workflows");
        for entry in std::fs::read_dir(root).unwrap() {
            let path = entry.unwrap().path();
            if !matches!(
                path.extension().and_then(|s| s.to_str()),
                Some("yml" | "yaml")
            ) {
                continue;
            }
            let text = std::fs::read_to_string(&path)
                .unwrap()
                .replace("\r\n", "\n");
            let mut job_env = false;
            for line in text.lines() {
                if line == "    env:" {
                    job_env = true;
                } else if !line.trim().is_empty()
                    && !line.trim_start().starts_with('#')
                    && line.len() - line.trim_start().len() <= 4
                {
                    job_env = false;
                }
                if job_env {
                    assert!(
                        !line.contains("runner."),
                        "{}: runner context unavailable in job env: {line}",
                        path.display()
                    );
                }
            }
            if path.file_name().unwrap() == "tests.yml" {
                let worker = text
                    .split("\n  rust-worker:\n")
                    .nth(1)
                    .unwrap()
                    .split("\n  rust-spike:\n")
                    .next()
                    .unwrap();
                assert!(worker.contains("--bin ci-temporal -- cargo test"));
            }
            for forbidden in [concat!("17", "233"), concat!("/tmp/", "temporal.log")] {
                assert!(
                    !text.contains(forbidden),
                    "{} contains {forbidden}",
                    path.display()
                );
            }
        }
    }
}
