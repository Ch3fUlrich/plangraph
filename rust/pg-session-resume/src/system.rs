//! The real host: processes, /proc, git, the Claude transcript directory.

use crate::contract::mtime_secs;
use crate::host::{Host, JobState};
use crate::record::{Agent, Record};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};

pub struct System {
    pub log_file: PathBuf,
    /// Set when a log line could not be persisted; the process then exits non-zero.
    pub log_failed: bool,
}

fn git(cwd: &Path, args: &[&str]) -> Option<String> {
    let out = Command::new("git")
        .arg("-C")
        .arg(cwd)
        .args(args)
        .output()
        .ok()?;
    out.status
        .success()
        .then(|| String::from_utf8_lossy(&out.stdout).trim().to_owned())
}

/// Claude Code stores a transcript at <home>/.claude/projects/<cwd, non-alphanumerics as '-'>/<id>.jsonl.
pub fn transcript_path(home: &Path, cwd: &str, session_id: &str) -> PathBuf {
    let dir: String = cwd
        .chars()
        .map(|c| if c.is_ascii_alphanumeric() { c } else { '-' })
        .collect();
    home.join(".claude/projects")
        .join(dir)
        .join(format!("{session_id}.jsonl"))
}

impl Host for System {
    fn now(&self) -> u64 {
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map_or(0, |d| d.as_secs())
    }

    fn run(&mut self, cwd: Option<&Path>, argv: &[String]) -> Result<String, String> {
        let mut cmd = Command::new(&argv[0]);
        cmd.args(&argv[1..]).stdin(Stdio::null());
        if let Some(cwd) = cwd {
            cmd.current_dir(cwd);
        }
        let out = cmd.output().map_err(|e| format!("{}: {e}", argv[0]))?;
        if out.status.success() {
            return Ok(String::from_utf8_lossy(&out.stdout).into_owned());
        }
        Err(format!(
            "{} exit {}: {}",
            argv[0],
            out.status,
            String::from_utf8_lossy(&out.stderr)
        ))
    }

    fn spawn(&mut self, cwd: &Path, argv: &[String]) -> Result<u32, String> {
        Command::new(&argv[0])
            .args(&argv[1..])
            .current_dir(cwd)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .map(|child| child.id())
            .map_err(|e| format!("{}: {e}", argv[0]))
    }

    fn proc_start(&self, pid: u32) -> Option<u64> {
        proc_start_ticks(&std::fs::read_to_string(format!("/proc/{pid}/stat")).ok()?)
    }

    fn mem_available_mb(&self) -> Option<u64> {
        let text = std::fs::read_to_string("/proc/meminfo").ok()?;
        let line = text.lines().find(|l| l.starts_with("MemAvailable:"))?;
        Some(line.split_whitespace().nth(1)?.parse::<u64>().ok()? / 1024)
    }

    fn git_head(&self, cwd: &Path) -> Option<String> {
        git(cwd, &["rev-parse", "HEAD"])
    }

    fn git_dirty(&self, cwd: &Path) -> Option<bool> {
        git(cwd, &["status", "--porcelain"]).map(|s| !s.is_empty())
    }

    fn activity_ts(&self, rec: &Record) -> Option<u64> {
        let home = PathBuf::from(std::env::var_os("HOME")?);
        let newest = |paths: Vec<PathBuf>| paths.iter().filter_map(|p| mtime_secs(p)).max();
        if let Some(own) = rec.str("activity_path") {
            return newest(vec![PathBuf::from(own)]); // the launcher names the file the runtime writes
        }
        match rec.agent() {
            Agent::Claude => {
                let transcript = transcript_path(&home, rec.str("cwd")?, rec.str("session_id")?);
                newest(vec![transcript, job_dir(&home, rec)?.join("state.json")])
            }
            Agent::Codex => newest(codex_rollouts(&home, rec.handle()?)),
            Agent::OpenCode => None, // no known activity file: such a session is never reaped as idle
        }
    }

    fn job_state(&self, rec: &Record) -> Option<JobState> {
        let home = PathBuf::from(std::env::var_os("HOME")?);
        let text = std::fs::read_to_string(job_dir(&home, rec)?.join("state.json")).ok()?;
        parse_job_state(&text)
    }

    fn log(&mut self, line: &str) {
        let line = format!("ts={} {line}", self.now());
        println!("{line}");
        let appended = std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(&self.log_file)
            .and_then(|mut f| writeln!(f, "{line}"));
        if let Err(e) = appended {
            eprintln!(
                "pg-session-resume: cannot append to {}: {e}",
                self.log_file.display()
            );
            self.log_failed = true;
        }
    }
}

fn job_dir(home: &Path, rec: &Record) -> Option<PathBuf> {
    Some(home.join(".claude/jobs").join(rec.job_id()?))
}

/// `rollout-*-<thread>.jsonl` under ~/.codex/sessions/<year>/<month>/<day>/.
fn codex_rollouts(home: &Path, thread: &str) -> Vec<PathBuf> {
    let list = |dir: &Path| -> Vec<PathBuf> {
        std::fs::read_dir(dir)
            .into_iter()
            .flatten()
            .flatten()
            .map(|e| e.path())
            .collect()
    };
    let days = list(&home.join(".codex/sessions"))
        .iter()
        .flat_map(|y| list(y))
        .flat_map(|m| list(&m))
        .collect::<Vec<_>>();
    days.iter()
        .flat_map(|d| list(d))
        .filter(|f| {
            f.file_name().and_then(|n| n.to_str()).is_some_and(|n| {
                n.starts_with("rollout-") && n.ends_with(&format!("{thread}.jsonl"))
            })
        })
        .collect()
}

pub fn parse_job_state(text: &str) -> Option<JobState> {
    let v: serde_json::Value = serde_json::from_str(text).ok()?;
    let state = v.get("state")?.as_str()?.to_owned();
    let tasks = v
        .get("inFlight")
        .and_then(|i| i.get("tasks"))
        .and_then(|t| t.as_u64())
        .unwrap_or(0);
    Some(JobState { state, tasks })
}

/// Field 22 of /proc/<pid>/stat (start time in clock ticks); the command name in
/// field 2 may hold spaces and parentheses, so count from the last `)`.
pub fn proc_start_ticks(stat: &str) -> Option<u64> {
    stat[stat.rfind(')')? + 1..]
        .split_whitespace()
        .nth(19)?
        .parse()
        .ok()
}
