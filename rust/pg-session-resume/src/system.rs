//! The real host: processes, /proc, git, the Claude transcript directory.

use crate::host::Host;
use crate::record::Record;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};

pub struct System {
    pub log_file: PathBuf,
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

    fn spawn(&mut self, cwd: &Path, argv: &[String]) -> Result<(), String> {
        Command::new(&argv[0])
            .args(&argv[1..])
            .current_dir(cwd)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .map(drop)
            .map_err(|e| format!("{}: {e}", argv[0]))
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
        let path = transcript_path(&home, rec.str("cwd")?, rec.str("session_id")?);
        let m = std::fs::metadata(path).ok()?.modified().ok()?;
        Some(m.duration_since(std::time::UNIX_EPOCH).ok()?.as_secs())
    }

    fn log(&mut self, line: &str) {
        let line = format!("ts={} {line}", self.now());
        println!("{line}");
        if let Ok(mut f) = std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(&self.log_file)
        {
            let _ = writeln!(f, "{line}");
        }
    }
}
