//! Keeper client: the worker's only channel to orchestration.
//!
//! The real keeper service does not exist yet, so this contract is the seam:
//! an HTTP client for production and an in-memory stub for tests both
//! implement [`Keeper`].
//!
//! | Method | Path           | Request body             | Success response   |
//! |--------|----------------|--------------------------|--------------------|
//! | POST   | `/v1/claim`    | `{"worker": ..}`         | 200 `Claim` \| 204 |
//! | POST   | `/v1/attempts` | `AttemptReport`          | 204                |
//! | POST   | `/v1/park`     | `ParkPacket`             | 204                |
//! | POST   | `/v1/complete` | `{"card", "merged_sha"}` | 204                |
//! | POST   | `/v1/alarms`   | `Alarm`                  | 204                |
//!
//! Any other status is reported as [`KeeperError::Status`].

use pg_worker_core::exit_class::ExitClass;
use pg_worker_core::packet::ParkPacket;
use serde::{Deserialize, Serialize};
use std::sync::Mutex;
use std::time::Duration;

/// One unit of work handed to a worker.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Claim {
    pub card: String,
    pub repo: String,
    pub base_sha: String,
    pub check_cmd: String,
    pub allow: Vec<String>,
    pub forbid: Vec<String>,
    pub prompt: String,
}

/// What one attempt did, sent back so the keeper can pick the next move.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct AttemptReport {
    pub card: String,
    pub n: u32,
    pub leg: String,
    pub exit_class: ExitClass,
    pub diff_hash: String,
    pub check_tail: String,
}

/// A condition a human must look at.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Alarm {
    pub card: String,
    pub reason: String,
}

#[derive(Debug, thiserror::Error, PartialEq, Eq)]
pub enum KeeperError {
    #[error("keeper request failed: {0}")]
    Http(String),
    #[error("keeper returned unexpected status {0}")]
    Status(u16),
    #[error("keeper response could not be decoded: {0}")]
    Decode(String),
}

/// The worker's view of orchestration. Implemented by [`HttpKeeper`] against
/// the real service and by [`StubKeeper`] in tests.
pub trait Keeper: Send + Sync {
    fn claim_next(&self, worker: &str) -> Result<Option<Claim>, KeeperError>;
    fn report_attempt(&self, r: &AttemptReport) -> Result<(), KeeperError>;
    fn park(&self, p: &ParkPacket) -> Result<(), KeeperError>;
    fn complete(&self, card: &str, merged_sha: &str) -> Result<(), KeeperError>;
    fn alarm(&self, a: &Alarm) -> Result<(), KeeperError>;
}

/// Blocking HTTP implementation of [`Keeper`] over ureq.
pub struct HttpKeeper {
    base: String,
    agent: ureq::Agent,
}

impl HttpKeeper {
    /// `base_url` may carry a trailing slash; it is stripped so paths can be
    /// appended verbatim.
    pub fn new(base_url: &str) -> Self {
        let base = base_url.trim_end_matches('/').to_string();
        let agent = ureq::AgentBuilder::new()
            .timeout(Duration::from_secs(10))
            .build();
        HttpKeeper { base, agent }
    }

    fn url(&self, path: &str) -> String {
        format!("{}{}", self.base, path)
    }

    /// POST a JSON body whose only accepted success code is 204.
    fn post204(&self, path: &str, body: &impl Serialize) -> Result<(), KeeperError> {
        let url = self.url(path);
        match self.agent.post(&url).send_json(body) {
            Ok(resp) if resp.status() == 204 => Ok(()),
            Ok(resp) => Err(KeeperError::Status(resp.status())),
            Err(ureq::Error::Status(code, _)) => Err(KeeperError::Status(code)),
            Err(e) => Err(KeeperError::Http(e.to_string())),
        }
    }
}

impl Keeper for HttpKeeper {
    fn claim_next(&self, worker: &str) -> Result<Option<Claim>, KeeperError> {
        let url = self.url("/v1/claim");
        let payload = serde_json::json!({ "worker": worker });
        match self.agent.post(&url).send_json(payload) {
            Ok(resp) => match resp.status() {
                200 => {
                    let claim: Claim = resp
                        .into_json()
                        .map_err(|e| KeeperError::Decode(e.to_string()))?;
                    Ok(Some(claim))
                }
                // No work available right now.
                204 => Ok(None),
                code => Err(KeeperError::Status(code)),
            },
            Err(ureq::Error::Status(code, _)) => Err(KeeperError::Status(code)),
            Err(e) => Err(KeeperError::Http(e.to_string())),
        }
    }

    fn report_attempt(&self, r: &AttemptReport) -> Result<(), KeeperError> {
        self.post204("/v1/attempts", r)
    }

    fn park(&self, p: &ParkPacket) -> Result<(), KeeperError> {
        self.post204("/v1/park", p)
    }

    fn complete(&self, card: &str, merged_sha: &str) -> Result<(), KeeperError> {
        let payload = serde_json::json!({ "card": card, "merged_sha": merged_sha });
        self.post204("/v1/complete", &payload)
    }

    fn alarm(&self, a: &Alarm) -> Result<(), KeeperError> {
        self.post204("/v1/alarms", a)
    }
}

/// In-memory [`Keeper`] for tests: claims come from a queue the test fills, and
/// every other call is recorded for later assertions.
#[derive(Default)]
pub struct StubKeeper {
    inner: Mutex<StubState>,
}

#[derive(Default)]
struct StubState {
    queue: Vec<Claim>,
    attempts: Vec<AttemptReport>,
    parked: Vec<ParkPacket>,
    completed: Vec<(String, String)>,
    alarms: Vec<Alarm>,
}

impl StubKeeper {
    pub fn push_claim(&self, c: Claim) {
        self.lock().queue.push(c);
    }

    pub fn attempts(&self) -> Vec<AttemptReport> {
        self.lock().attempts.clone()
    }

    pub fn parked(&self) -> Vec<ParkPacket> {
        self.lock().parked.clone()
    }

    pub fn completed(&self) -> Vec<(String, String)> {
        self.lock().completed.clone()
    }

    pub fn alarms(&self) -> Vec<Alarm> {
        self.lock().alarms.clone()
    }

    fn lock(&self) -> std::sync::MutexGuard<'_, StubState> {
        self.inner.lock().expect("stub keeper mutex poisoned")
    }
}

impl Keeper for StubKeeper {
    fn claim_next(&self, _worker: &str) -> Result<Option<Claim>, KeeperError> {
        let mut state = self.lock();
        if state.queue.is_empty() {
            Ok(None)
        } else {
            Ok(Some(state.queue.remove(0)))
        }
    }

    fn report_attempt(&self, r: &AttemptReport) -> Result<(), KeeperError> {
        self.lock().attempts.push(r.clone());
        Ok(())
    }

    fn park(&self, p: &ParkPacket) -> Result<(), KeeperError> {
        self.lock().parked.push(p.clone());
        Ok(())
    }

    fn complete(&self, card: &str, merged_sha: &str) -> Result<(), KeeperError> {
        self.lock()
            .completed
            .push((card.to_string(), merged_sha.to_string()));
        Ok(())
    }

    fn alarm(&self, a: &Alarm) -> Result<(), KeeperError> {
        self.lock().alarms.push(a.clone());
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use pg_worker_core::config::Tier;
    use std::io::{Read, Write};
    use std::net::{TcpListener, TcpStream};
    use std::thread::JoinHandle;

    fn claim() -> Claim {
        Claim {
            card: "c-1".into(),
            repo: "org/repo".into(),
            base_sha: "abc123".into(),
            check_cmd: "cargo test".into(),
            allow: vec!["src/**".into()],
            forbid: vec![".env".into()],
            prompt: "do the thing".into(),
        }
    }

    fn report() -> AttemptReport {
        AttemptReport {
            card: "c-1".into(),
            n: 2,
            leg: "free".into(),
            exit_class: ExitClass::CheckFail,
            diff_hash: "deadbeef".into(),
            check_tail: "boom".into(),
        }
    }

    fn park() -> ParkPacket {
        ParkPacket {
            card: "c-1".into(),
            tier: Tier::Free,
            exit_class: ExitClass::CheckFail,
            check_tail: "boom".into(),
        }
    }

    fn alarm() -> Alarm {
        Alarm {
            card: "c-1".into(),
            reason: "stuck".into(),
        }
    }

    #[test]
    fn stub_keeper_round_trip() {
        let keeper = StubKeeper::default();
        assert_eq!(keeper.claim_next("w-1").unwrap(), None);

        let c = claim();
        keeper.push_claim(c.clone());
        assert_eq!(keeper.claim_next("w-1").unwrap(), Some(c));
        assert_eq!(keeper.claim_next("w-1").unwrap(), None);

        let r = report();
        keeper.report_attempt(&r).unwrap();
        assert_eq!(keeper.attempts(), vec![r.clone()]);

        let p = park();
        keeper.park(&p).unwrap();
        assert_eq!(keeper.parked(), vec![p.clone()]);

        assert_eq!(keeper.completed(), Vec::<(String, String)>::new());
        keeper.complete("c-1", "sha-1").unwrap();
        assert_eq!(
            keeper.completed(),
            vec![("c-1".to_string(), "sha-1".to_string())]
        );

        let a = alarm();
        keeper.alarm(&a).unwrap();
        assert_eq!(keeper.alarms(), vec![a]);
    }

    /// A request as the one-shot test server observed it.
    struct Captured {
        method: String,
        path: String,
        body: String,
    }

    /// Bind a one-shot HTTP server on 127.0.0.1:0. It accepts a single
    /// request, reads it fully (headers plus `Content-Length` body), replies
    /// with the canned status line and body, and yields what it saw.
    fn serve_once(status: u16, reason: &str, body: &str) -> (String, JoinHandle<Captured>) {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind ephemeral port");
        let addr = listener.local_addr().expect("read local addr");
        let reason = reason.to_string();
        let body = body.to_string();
        let handle = std::thread::spawn(move || {
            let (mut stream, _) = listener.accept().expect("accept one request");
            let captured = read_request(&mut stream);
            let mut response = format!("HTTP/1.1 {status} {reason}\r\n");
            // 204 carries no Content-Length and no body by definition.
            if status != 204 {
                response.push_str(&format!("Content-Length: {}\r\n", body.len()));
            }
            response.push_str("Content-Type: application/json\r\nConnection: close\r\n\r\n");
            response.push_str(&body);
            stream
                .write_all(response.as_bytes())
                .expect("write response");
            stream.flush().expect("flush response");
            captured
        });
        (format!("http://{addr}"), handle)
    }

    fn read_request(stream: &mut TcpStream) -> Captured {
        // Headers end at the first blank line; read a byte at a time so a
        // request split across TCP segments is still captured whole.
        let mut raw = Vec::new();
        let mut byte = [0u8; 1];
        while !raw.ends_with(b"\r\n\r\n") {
            match stream.read(&mut byte) {
                Ok(0) => break,
                Ok(_) => raw.push(byte[0]),
                Err(_) => break,
            }
        }
        let head = String::from_utf8_lossy(&raw).to_string();
        let request_line = head.lines().next().unwrap_or_default();
        let mut parts = request_line.split_whitespace();
        let method = parts.next().unwrap_or_default().to_string();
        let path = parts.next().unwrap_or_default().to_string();
        let content_length = head
            .lines()
            .find_map(|line| {
                let (name, value) = line.split_once(':')?;
                if name.eq_ignore_ascii_case("content-length") {
                    value.trim().parse::<usize>().ok()
                } else {
                    None
                }
            })
            .unwrap_or(0);
        let mut body = vec![0u8; content_length];
        if content_length > 0 {
            stream.read_exact(&mut body).expect("read request body");
        }
        Captured {
            method,
            path,
            body: String::from_utf8_lossy(&body).to_string(),
        }
    }

    #[test]
    fn http_claim_200_returns_claim() {
        let c = claim();
        let body = serde_json::to_string(&c).unwrap();
        let (base, server) = serve_once(200, "OK", &body);
        let keeper = HttpKeeper::new(&base);

        assert_eq!(keeper.claim_next("w-1").unwrap(), Some(c));

        let captured = server.join().expect("server thread");
        assert_eq!(captured.method, "POST");
        assert_eq!(captured.path, "/v1/claim");
        assert_eq!(
            serde_json::from_str::<serde_json::Value>(&captured.body).unwrap(),
            serde_json::json!({ "worker": "w-1" })
        );
    }

    #[test]
    fn http_claim_204_is_none() {
        let (base, server) = serve_once(204, "No Content", "");
        let keeper = HttpKeeper::new(&base);

        assert_eq!(keeper.claim_next("w-1").unwrap(), None);
        server.join().expect("server thread");
    }

    #[test]
    fn http_report_attempt_posts_to_attempts() {
        let r = report();
        let (base, server) = serve_once(204, "No Content", "");
        // A trailing slash on the base must not double up in the path.
        let keeper = HttpKeeper::new(&format!("{base}/"));

        keeper.report_attempt(&r).unwrap();

        let captured = server.join().expect("server thread");
        assert_eq!(captured.method, "POST");
        assert_eq!(captured.path, "/v1/attempts");
        let sent: AttemptReport = serde_json::from_str(&captured.body).unwrap();
        assert_eq!(sent, r);
        assert!(captured.body.contains("\"exit_class\":\"check_fail\""));
    }

    #[test]
    fn http_status_error_maps_to_status() {
        let (base, server) = serve_once(500, "Internal Server Error", "");
        let keeper = HttpKeeper::new(&base);

        let err = keeper.report_attempt(&report()).unwrap_err();
        assert_eq!(err, KeeperError::Status(500));
        server.join().expect("server thread");
    }

    #[test]
    fn http_remaining_endpoints() {
        let (base, server) = serve_once(204, "No Content", "");
        HttpKeeper::new(&base).park(&park()).unwrap();
        assert_eq!(server.join().unwrap().path, "/v1/park");

        let (base, server) = serve_once(204, "No Content", "");
        HttpKeeper::new(&base).complete("c-1", "sha-1").unwrap();
        let captured = server.join().unwrap();
        assert_eq!(captured.path, "/v1/complete");
        assert_eq!(
            serde_json::from_str::<serde_json::Value>(&captured.body).unwrap(),
            serde_json::json!({ "card": "c-1", "merged_sha": "sha-1" })
        );

        let (base, server) = serve_once(204, "No Content", "");
        HttpKeeper::new(&base).alarm(&alarm()).unwrap();
        assert_eq!(server.join().unwrap().path, "/v1/alarms");
    }
}
