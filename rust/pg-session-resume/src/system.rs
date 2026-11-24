//! The real host: processes, /proc, git, the runtimes' own state files.

use crate::host::{Host, JobState, Process};
use crate::record::{Agent, Record};
use std::io::Write;
use std::os::fd::{AsRawFd, FromRawFd, OwnedFd};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};

/// How long a signalled process may take to exit before the stop counts as failed.
pub const EXIT_WAIT_MS: i32 = 10_000;

fn pidfd_open(pid: u32) -> std::io::Result<OwnedFd> {
    // SAFETY: pidfd_open takes a pid and zero flags and returns a new descriptor we own.
    let fd = unsafe { libc::syscall(libc::SYS_pidfd_open, pid, 0) };
    if fd < 0 {
        return Err(std::io::Error::last_os_error());
    }
    // SAFETY: fd is a fresh, valid descriptor.
    Ok(unsafe { OwnedFd::from_raw_fd(fd as i32) })
}

pub struct System {
    /// Milliseconds `terminate` waits for the process to exit (`EXIT_WAIT_MS`).
    pub exit_wait_ms: i32,
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

    fn process(&self, pid: u32) -> Process {
        match std::fs::read_to_string(format!("/proc/{pid}/stat")) {
            Ok(stat) => match (proc_state(&stat), proc_start_ticks(&stat)) {
                // An exited process nobody waited for still has a /proc entry: gone.
                (Some('Z' | 'X'), _) => Process::Gone,
                (Some(_), Some(start)) => Process::Live(start),
                _ => Process::Unknown,
            },
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Process::Gone,
            Err(_) => Process::Unknown,
        }
    }

    fn terminate(&mut self, pid: u32, start: u64) -> Result<(), String> {
        let fd = match pidfd_open(pid) {
            Ok(fd) => fd,
            Err(e) if e.raw_os_error() == Some(libc::ESRCH) => return Ok(()),
            Err(e) => return Err(format!("pidfd_open {pid}: {e}")),
        };
        // The descriptor now pins one process: check it is the recorded one, then signal
        // through the descriptor, so a pid reused in between is never hit.
        match self.process(pid) {
            Process::Live(s) if s == start => {}
            Process::Gone => return Ok(()),
            Process::Live(_) => return Err(format!("pid {pid} belongs to another process")),
            Process::Unknown => return Err(format!("pid {pid} state unreadable")),
        }
        let sent = unsafe {
            // SAFETY: signals only the process the descriptor pins; no siginfo, no flags.
            libc::syscall(
                libc::SYS_pidfd_send_signal,
                fd.as_raw_fd(),
                libc::SIGTERM,
                std::ptr::null::<libc::siginfo_t>(),
                0,
            )
        };
        if sent < 0 {
            let e = std::io::Error::last_os_error();
            return if e.raw_os_error() == Some(libc::ESRCH) {
                Ok(())
            } else {
                Err(format!("signal {pid}: {e}"))
            };
        }
        // A pidfd becomes readable when its process exits: wait for that, never assume it.
        let mut p = libc::pollfd {
            fd: fd.as_raw_fd(),
            events: libc::POLLIN,
            revents: 0,
        };
        // SAFETY: one valid pollfd on a descriptor this function owns.
        let ready = unsafe { libc::poll(&mut p, 1, self.exit_wait_ms) };
        if ready > 0 {
            Ok(())
        } else {
            Err(format!(
                "pid {pid} still running {}s after SIGTERM",
                EXIT_WAIT_MS / 1000
            ))
        }
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
        // Nanoseconds, and strictly after: a marker from the same instant as the activity (or
        // the same second) cannot prove that the turn the activity belongs to has ended.
        let marker = mtime_ns(Path::new(rec.str("turn_end_path")?))?;
        Some(marker > self.activity_ns(rec)?)
    }

    fn mem_available_mb(&self) -> Option<u64> {
        let text = std::fs::read_to_string("/proc/meminfo").ok()?;
        let line = text.lines().find(|l| l.starts_with("MemAvailable:"))?;
        Some(line.split_whitespace().nth(1)?.parse::<u64>().ok()? / 1024)
    }

    fn git_head(&self, cwd: &Path) -> Option<String> {
        git(cwd, &["rev-parse", "HEAD"])
    }

    fn activity_ts(&self, rec: &Record) -> Option<u64> {
        u64::try_from(self.activity_ns(rec)? / 1_000_000_000).ok()
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

impl System {
    /// Last sign of life in nanoseconds since the epoch.
    fn activity_ns(&self, rec: &Record) -> Option<u128> {
        let newest = |paths: Vec<PathBuf>| paths.iter().filter_map(|p| mtime_ns(p)).max();
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
}

fn mtime_ns(path: &Path) -> Option<u128> {
    let m = std::fs::metadata(path).ok()?.modified().ok()?;
    Some(m.duration_since(std::time::UNIX_EPOCH).ok()?.as_nanos())
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

/// The job state. `tasks` counts the running tasks and the queued work together and is None
/// when the file does not carry valid counts: a malformed count is never read as zero.
pub fn parse_job_state(text: &str) -> Option<JobState> {
    let v: serde_json::Value = serde_json::from_str(text).ok()?;
    let state = v.get("state")?.as_str()?.to_owned();
    let count = |flight: &serde_json::Value, key: &str| match flight.get(key) {
        None => Some(0), // queued work is not reported by every runtime version
        Some(n) => n.as_u64(),
    };
    let tasks = match v.get("inFlight") {
        Some(f) if f.is_object() => f
            .get("tasks")
            .and_then(serde_json::Value::as_u64)
            .zip(count(f, "queued"))
            .zip(count(f, "drainableMonitors"))
            .map(|((t, q), m)| t + q + m),
        _ => None,
    };
    Some(JobState { state, tasks })
}

/// Field 3 of /proc/<pid>/stat (the state letter), counted from the last `)`.
pub fn proc_state(stat: &str) -> Option<char> {
    stat[stat.rfind(')')? + 1..]
        .split_whitespace()
        .next()?
        .chars()
        .next()
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
