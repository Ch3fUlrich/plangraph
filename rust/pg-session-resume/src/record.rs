//! One launch record (`<state>/launch/sessions/<session_name>.json`). The file is
//! shared with the launcher and the Python spawner, so unknown fields are kept.

use serde_json::Value;
use std::path::{Path, PathBuf};

pub struct Record {
    pub path: PathBuf,
    pub value: Value,
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Agent {
    Claude,
    Codex,
    OpenCode,
}

impl Agent {
    pub fn name(self) -> &'static str {
        match self {
            Agent::Claude => "claude",
            Agent::Codex => "codex",
            Agent::OpenCode => "opencode",
        }
    }
}

impl Record {
    pub fn str(&self, key: &str) -> Option<&str> {
        self.value.get(key)?.as_str().filter(|s| !s.is_empty())
    }

    pub fn u64(&self, key: &str) -> Option<u64> {
        self.value.get(key)?.as_u64()
    }

    /// The name hooks and humans use: session_name, else name, else the file stem.
    pub fn label(&self) -> String {
        self.str("session_name")
            .or_else(|| self.str("name"))
            .map(str::to_owned)
            .or_else(|| self.path.file_stem()?.to_str().map(str::to_owned))
            .unwrap_or_default()
    }

    pub fn agent(&self) -> Agent {
        match self.str("agent").or_else(|| self.str("harness")) {
            Some("codex") => Agent::Codex,
            Some("opencode") => Agent::OpenCode,
            _ => Agent::Claude,
        }
    }

    /// The lane: the `lane` field, else the last component of the worktree path.
    pub fn lane(&self) -> Option<String> {
        let lane = self.str("lane").map(str::to_owned);
        lane.or_else(|| {
            Path::new(self.str("cwd")?)
                .file_name()?
                .to_str()
                .map(str::to_owned)
        })
    }

    /// Whether this launch was stopped. A stop belongs to the launch that produced it: a respawn
    /// copies the old record under a newer `ts` (and a new run id and handle), so the stop marks
    /// it carries say nothing about the new session.
    pub fn stopped(&self) -> bool {
        let stopped_ts = self.u64("stopped_ts");
        let marked = self.value.get("stopped_ts").is_some_and(|v| !v.is_null())
            || self.str("state") == Some("stopped");
        if !marked {
            return false;
        }
        match self.resume() {
            Some(r) => {
                r.get("launch_ts").and_then(Value::as_u64) == self.u64("ts")
                    && r.get("launch_run").and_then(Value::as_str) == self.str("run_id")
                    && r.get("handle").and_then(Value::as_str) == self.handle()
            }
            // A stop mark without our resume object: the launch must not be newer than it.
            None => match (self.u64("ts"), stopped_ts) {
                (Some(ts), Some(stop)) => ts <= stop,
                _ => true,
            },
        }
    }

    /// The id the agent resumes by: Claude session id, Codex thread id, OpenCode session.
    pub fn handle(&self) -> Option<&str> {
        let key = match self.agent() {
            Agent::Claude => "session_id",
            Agent::Codex => "thread_id",
            Agent::OpenCode => "opencode_session",
        };
        self.str(key).filter(|h| {
            h.bytes()
                .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')
        })
    }

    /// The 8-char Claude job id: `job_id`, else the head of the session id.
    pub fn job_id(&self) -> Option<String> {
        let job = self
            .str("job_id")
            .or_else(|| self.handle().map(|h| &h[..h.len().min(8)]))?;
        job.bytes()
            .all(|b| b.is_ascii_alphanumeric())
            .then(|| job.to_owned())
    }

    pub fn resume(&self) -> Option<&Value> {
        self.value.get("resume").filter(|v| v.is_object())
    }

    pub fn resume_str(&self, key: &str) -> Option<&str> {
        self.resume()?.get(key)?.as_str()
    }

    pub fn resume_u64(&self, key: &str) -> Option<u64> {
        self.resume()?.get(key)?.as_u64()
    }

    pub fn resume_round(&self) -> u64 {
        self.resume()
            .and_then(|r| r.get("round")?.as_u64())
            .unwrap_or(0)
    }

    pub fn set(&mut self, key: &str, v: Value) {
        if let Some(map) = self.value.as_object_mut() {
            map.insert(key.to_owned(), v);
        }
    }

    pub fn save(&self) -> std::io::Result<()> {
        write_private(
            &self.path,
            serde_json::to_string_pretty(&self.value)?.as_bytes(),
        )
    }
}

/// Replace `path` atomically with `bytes`, readable by the owner only from the first byte on:
/// the temp file (`<path>.tmp`) is created 0600, whatever the umask. These files carry resume
/// handles and worktree paths.
pub fn write_private(path: &Path, bytes: &[u8]) -> std::io::Result<()> {
    use std::io::Write;
    use std::os::unix::fs::OpenOptionsExt;
    let mut tmp = path.as_os_str().to_owned();
    tmp.push(".tmp");
    let tmp = PathBuf::from(tmp);
    let mut file = std::fs::OpenOptions::new()
        .write(true)
        .create(true)
        .truncate(true)
        .mode(0o600)
        .open(&tmp)?;
    file.write_all(bytes)?;
    file.sync_all()?;
    std::fs::rename(tmp, path)
}

/// Every readable record of the directory in name order; a broken file is skipped.
pub fn load_all(dir: &Path) -> Vec<Record> {
    let mut paths: Vec<PathBuf> = std::fs::read_dir(dir)
        .into_iter()
        .flatten()
        .flatten()
        .map(|e| e.path())
        .filter(|p| p.extension().is_some_and(|x| x == "json"))
        .collect();
    paths.sort();
    paths
        .into_iter()
        .filter_map(|path| {
            let value: Value = serde_json::from_slice(&std::fs::read(&path).ok()?).ok()?;
            value.is_object().then_some(Record { path, value })
        })
        .collect()
}
