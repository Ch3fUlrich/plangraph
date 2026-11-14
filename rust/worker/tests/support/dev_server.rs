//! Private Temporal dev server; no external service or CI wiring required.
use std::{
    path::PathBuf,
    process::{Child, Command, Stdio},
    time::Duration,
};

pub struct DevServer {
    child: Child,
    _dir: tempfile::TempDir,
    pub addr: String,
}

impl DevServer {
    pub fn start() -> Option<Self> {
        let explicit =
            std::env::var_os("PG_TEMPORAL_CLI").or_else(|| std::env::var_os("TEMPORAL_CLI"));
        let cli = explicit.map(PathBuf::from).or_else(|| {
            std::env::split_paths(&std::env::var_os("PATH").unwrap_or_default())
                .map(|p| p.join("temporal"))
                .find(|p| p.is_file())
        });
        let Some(cli) = cli else {
            eprintln!(
                "SKIP[temporal-dev-server-missing]: install temporal or set PG_TEMPORAL_CLI/TEMPORAL_CLI"
            );
            return None;
        };
        let dir = tempfile::tempdir().unwrap();
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        drop(listener);
        let log_path = dir.path().join("server.log");
        let log = std::fs::File::create(&log_path).unwrap();
        let child = Command::new(cli)
            .args([
                "server",
                "start-dev",
                "--ip",
                "127.0.0.1",
                "--port",
                &port.to_string(),
                "--headless",
            ])
            .stdout(Stdio::from(log.try_clone().unwrap()))
            .stderr(Stdio::from(log))
            .spawn()
            .expect("start Temporal dev server");
        let server = Self {
            child,
            _dir: dir,
            addr: format!("http://127.0.0.1:{port}"),
        };
        for _ in 0..200 {
            if std::net::TcpStream::connect(("127.0.0.1", port)).is_ok() {
                return Some(server);
            }
            std::thread::sleep(Duration::from_millis(100));
        }
        panic!(
            "Temporal dev server unavailable: {}",
            std::fs::read_to_string(log_path).unwrap()
        );
    }
}

impl Drop for DevServer {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}
