//! The real host: processes, /proc, git, the Claude transcript directory.

use crate::contract::mtime_secs;
use crate::host::{Host, JobState};
use crate::record::{Agent, Record};
use std::io::Write;
use std::os::unix::process::CommandExt;
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
        // Its own process group: the resumed writer outlives this tick (and a tick killed
        // with its group). The service unit uses KillMode=process for the same reason.
        Command::new(&argv[0])
            .args(&argv[1..])
            .current_dir(cwd)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .process_group(0)
            .spawn()
            .map(|child| child.id())
            .map_err(|e| format!("{}: {e}", argv[0]))
    }

    fn proc_start(&self, pid: u32) -> Option<u64> {
        proc_start_ticks(&std::fs::read_to_string(format!("/proc/{pid}/stat")).ok()?)
    }

    fn has_children(&self, pid: u32) -> Option<bool> {
        let mut readable = false;
        for entry in std::fs::read_dir("/proc").ok()?.flatten() {
            let Ok(stat) = std::fs::read_to_string(entry.path().join("stat")) else {
                continue;
            };
            readable = true;
            if parent_pid(&stat) == Some(u64::from(pid)) {
                return Some(true);
            }
        }
        readable.then_some(false)
    }

    fn worktree_id(&self, cwd: &Path) -> Option<String> {
        use std::os::unix::fs::MetadataExt;
        let meta = std::fs::metadata(cwd).ok()?;
        let git_dir = git(cwd, &["rev-parse", "--absolute-git-dir"])?;
        Some(format!("{}:{}:{git_dir}", meta.dev(), meta.ino()))
    }

    fn turn_ended(&self, rec: &Record) -> Option<bool> {
        let marker = mtime_secs(Path::new(rec.str("turn_end_path")?))?;
        Some(marker >= self.activity_ts(rec)?)
    }

    fn transcript_len(&self, rec: &Record) -> Option<u64> {
        Some(std::fs::metadata(transcript_file(rec)?).ok()?.len())
    }

    fn transcript_contains(&self, rec: &Record, from: u64, text: &str) -> Option<bool> {
        use std::io::{Read, Seek, SeekFrom};
        let mut file = std::fs::File::open(transcript_file(rec)?).ok()?;
        file.seek(SeekFrom::Start(from)).ok()?;
        let mut tail = Vec::new();
        file.read_to_end(&mut tail).ok()?;
        // The transcript holds the message JSON-escaped; compare in that form.
        let quoted = serde_json::to_string(text).ok()?;
        let needle = &quoted[1..quoted.len() - 1];
        Some(String::from_utf8_lossy(&tail).contains(needle))
    }

    fn find_process(&self, handle: &str) -> Option<(u32, u64)> {
        for entry in std::fs::read_dir("/proc").ok()?.flatten() {
            let pid: u32 = entry.file_name().to_str()?.parse().ok()?;
            let Ok(cmdline) = std::fs::read(entry.path().join("cmdline")) else {
                continue;
            };
            let args: Vec<&[u8]> = cmdline.split(|b| *b == 0).collect();
            let has = |want: &str| args.contains(&want.as_bytes());
            if has(handle) && (has("resume") || has("--resume") || has("--session")) {
                return Some((pid, self.proc_start(pid)?));
            }
        }
        None
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
        let newest = |paths: Vec<PathBuf>| paths.iter().filter_map(|p| mtime_secs(p)).max();
        if let Some(own) = rec.str("activity_path") {
            return newest(vec![PathBuf::from(own)]); // the launcher names the file the runtime writes
        }
        let home = PathBuf::from(std::env::var_os("HOME")?);
        match rec.agent() {
            Agent::Claude => {
                let transcript = transcript_path(&home, rec.str("cwd")?, rec.str("session_id")?);
                newest(vec![transcript, job_dir(&home, rec)?.join("state.json")])
            }
            Agent::Codex => newest(codex_rollouts(&home, rec.handle()?)),
            // OpenCode keeps one shared database: without a per-session `activity_path` the
            // session has no activity signal and is never reaped as idle.
            Agent::OpenCode => None,
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

/// The file the runtime appends the session's messages to.
fn transcript_file(rec: &Record) -> Option<PathBuf> {
    let home = PathBuf::from(std::env::var_os("HOME")?);
    match rec.agent() {
        Agent::Claude => Some(transcript_path(
            &home,
            rec.str("cwd")?,
            rec.str("session_id")?,
        )),
        Agent::Codex => codex_rollouts(&home, rec.handle()?)
            .into_iter()
            .max_by_key(|p| mtime_secs(p)),
        Agent::OpenCode => None,
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

/// Field 4 of /proc/<pid>/stat (the parent pid), counted from the last `)`.
pub fn parent_pid(stat: &str) -> Option<u64> {
    stat[stat.rfind(')')? + 1..]
        .split_whitespace()
        .nth(1)?
        .parse()
        .ok()
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
