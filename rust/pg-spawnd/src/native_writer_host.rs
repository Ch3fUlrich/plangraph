//! Target-host execution for the placed native-Codex run kind.
use crate::native_writer::{self, Driver};
use pg_worker_core::run::record_io::atomic_write_private;
use serde_json::{json, Value};
use std::{
    fs,
    io::{BufRead, BufReader, Read, Write},
    path::{Path, PathBuf},
    process::{Command, Stdio},
    time::Duration,
};

const WRITER_MODEL: &str = "gpt-6.1-sol";
const WRITER_EFFORT: &str = "medium";
/// Model and effort flags for the codex invocation; the ledger row records the same constants.
fn model_args() -> [String; 4] {
    [
        "-m".into(),
        WRITER_MODEL.into(),
        "-c".into(),
        format!("model_reasoning_effort={WRITER_EFFORT}"),
    ]
}

struct Host {
    repo: PathBuf,
    run_id: String,
    cwd: PathBuf,
    reviews: PathBuf,
    record_path: PathBuf,
    base_ref: String,
}
fn output(command: &mut Command) -> Result<String, String> {
    let result = command.output().map_err(|e| e.to_string())?;
    if !result.status.success() {
        return Err(format!(
            "command failed: {}",
            String::from_utf8_lossy(&result.stderr)
        ));
    }
    Ok(String::from_utf8_lossy(&result.stdout).trim().into())
}
impl Host {
    fn git(&self, args: &[&str]) -> Result<String, String> {
        output(Command::new("git").arg("-C").arg(&self.cwd).args(args))
    }
    fn ledger_row(&self, record: &Value) -> Result<Value, String> {
        let directory = self
            .record_path
            .parent()
            .ok_or("record directory missing")?;
        let log = match fs::read_to_string(directory.join("native.log")) {
            Ok(log) => log,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => String::new(),
            Err(e) => return Err(e.to_string()),
        };
        let item = record["item_key"].as_str();
        let mut lines = Value::Null;
        let mut done = item.map(|_| 0);
        if let (Some(base), Some(head)) =
            (record["base_sha"].as_str(), record["final_head"].as_str())
        {
            let stat = self.git(&["diff", "--numstat", base, head, "--"])?;
            let mut total = 0u64;
            for line in stat.lines() {
                for count in line.split('\t').take(2) {
                    if count != "-" {
                        total = total
                            .checked_add(count.parse::<u64>().map_err(|e| e.to_string())?)
                            .ok_or("changed lines overflow")?;
                    }
                }
            }
            lines = json!(total);
            if record["exit_code"] == 0 {
                let trailers = self.git(&[
                    "log",
                    "--format=%(trailers:key=Plan-Item,valueonly)",
                    &format!("{base}..{head}"),
                    "--",
                ])?;
                done = item.map(|key| u64::from(trailers.lines().any(|line| line.trim() == key)));
            }
        }
        let wall = record["ended"]
            .as_f64()
            .zip(record["started"].as_f64())
            .filter(|(end, start)| end >= start)
            .map(|(end, start)| (end - start) / 60.0);
        let accepted = record["review"]["verdict"] == "ACCEPT";
        Ok(
            json!({"schema_version":1,"kind":"writer","run_id":record["run_id"],
            "host":record["host"],"model":WRITER_MODEL,"effort":WRITER_EFFORT,
            "started":record["started"],"ended":record["ended"],"exit_code":record["exit_code"],
            "item_key":record["item_key"],"items_given":item.map(|_| 1),"items_done":done,
            "changed_lines":lines,"wall_minutes":wall,"tokens":crate::writer_ledger::tokens(&log)?,
            "review_rounds":record["review_rounds"],
            "high_held":accepted.then_some(0),"med_held":accepted.then_some(0)}),
        )
    }
    fn free(&self) -> Result<u64, String> {
        // State and repository may be on different filesystems. Probe the worktree's
        // nearest existing parent before creation, then the worktree itself.
        let mut path = self.cwd.as_path();
        while !path.exists() {
            path = path.parent().ok_or("worktree filesystem unavailable")?;
        }
        let text = output(Command::new("df").args(["-Pk", "--"]).arg(path))?;
        text.lines()
            .last()
            .and_then(|l| l.split_whitespace().nth(3))
            .and_then(|n| n.parse::<u64>().ok())
            .map(|kb| kb / (1024 * 1024))
            .ok_or_else(|| "disk probe failed".into())
    }
}
impl Driver for Host {
    fn disk_gb(&mut self) -> Result<u64, String> {
        self.free()
    }
    fn prepare(&mut self) -> Result<String, String> {
        if !self.base_ref.starts_with("refs/heads/") && !self.base_ref.starts_with("refs/tags/") {
            return Err("base_ref must name a pushed refs/heads/ or refs/tags/ ref".into());
        }
        output(
            Command::new("git")
                .arg("-C")
                .arg(&self.repo)
                .args(["check-ref-format", &self.base_ref]),
        )?;
        // Pin the pushed ref in a per-run namespace: parallel fetches cannot change this base.
        let pin = format!("refs/plangraph/native/{}/base", self.run_id);
        output(Command::new("git").arg("-C").arg(&self.repo).args([
            "fetch",
            "--no-write-fetch-head",
            "origin",
            &format!("+{}:{pin}", self.base_ref),
            "+refs/heads/main:refs/remotes/origin/main",
        ]))?;
        let base = output(
            Command::new("git")
                .arg("-C")
                .arg(&self.repo)
                .args(["rev-parse", &format!("{pin}^{{commit}}")]),
        )?;
        output(Command::new("git").arg("-C").arg(&self.repo).args([
            "merge-base",
            "--is-ancestor",
            "refs/remotes/origin/main",
            &base,
        ]))
        .map_err(|_| "stale base: pushed ref does not contain origin/main".to_owned())?;
        fs::create_dir_all(self.cwd.parent().ok_or("worktree parent missing")?)
            .map_err(|e| e.to_string())?;
        output(
            Command::new("git")
                .arg("-C")
                .arg(&self.repo)
                .args(["worktree", "add", "--detach"])
                .arg(&self.cwd)
                .arg(&base),
        )?;
        fs::create_dir_all(&self.reviews).map_err(|e| e.to_string())?;
        Ok(base)
    }
    fn execute(&mut self, session: Option<&str>, prompt: &str) -> Result<String, String> {
        let directory = self
            .record_path
            .parent()
            .ok_or("record directory missing")?;
        let log_path = directory.join("native.log");
        let log = fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(&log_path)
            .map_err(|e| e.to_string())?;
        let mut command = Command::new("codex");
        command.arg("exec");
        if let Some(id) = session {
            command.args(["resume", id]);
        }
        command
            .args(["--json"])
            .args(model_args())
            .args([
                "-c",
                "approval_policy=never",
                "-c",
                "sandbox_mode=\"danger-full-access\"",
                "-o",
            ])
            .arg(directory.join("native.last"))
            .arg("-")
            .current_dir(&self.cwd)
            .stdin(Stdio::piped())
            .stdout(log.try_clone().map_err(|e| e.to_string())?)
            .stderr(log);
        #[cfg(unix)]
        {
            use std::os::unix::process::CommandExt;
            command.process_group(0);
        }
        let mut child = command.spawn().map_err(|e| e.to_string())?;
        let payload = format!("{prompt}\nReview directory: {}\n", self.reviews.display());
        let mut stdin = child.stdin.take().ok_or("writer stdin missing")?;
        // A reader failure or disk probe failure must terminate the child too.
        let result = (|| {
            stdin
                .write_all(payload.as_bytes())
                .map_err(|e| e.to_string())?;
            drop(stdin);
            loop {
                if self.free()? < 2 {
                    return Err("disk_low: under 2 GB stop floor".into());
                }
                if let Some(status) = child.try_wait().map_err(|e| e.to_string())? {
                    if !status.success() {
                        return Err(format!("codex failed: {status}"));
                    }
                    break;
                }
                std::thread::sleep(Duration::from_secs(1));
            }
            if let Some(id) = session {
                return Ok(id.into());
            }
            let log = fs::File::open(&log_path).map_err(|e| e.to_string())?;
            BufReader::new(log.take(1024 * 1024))
                .lines()
                .map_while(Result::ok)
                .filter_map(|line| serde_json::from_str::<Value>(&line).ok())
                .find(|event| event["type"] == "thread.started")
                .and_then(|event| event["thread_id"].as_str().map(str::to_owned))
                .ok_or_else(|| "codex did not report a session id".into())
        })();
        if result.is_err() {
            #[cfg(unix)]
            unsafe {
                libc::killpg(child.id() as i32, libc::SIGKILL);
            }
            let _ = child.kill();
            let _ = child.wait();
        }
        result
    }
    fn head(&mut self) -> Result<String, String> {
        self.git(&["rev-parse", "HEAD"])
    }
    fn review(&mut self, head: &str) -> Result<Option<Value>, String> {
        find_review(&self.reviews, head)
    }
    fn gates(&mut self) -> Result<(), String> {
        self.git(&["diff", "--exit-code", "HEAD", "--"])?;
        if !self.cwd.join("tools/check_file_size.py").is_file() {
            return Ok(());
        }
        for gate in [
            "check_file_size.py",
            "check_new_code_lang.py",
            "check_changelog_docs.py",
        ] {
            let path = self.cwd.join("tools").join(gate);
            output(
                Command::new(if cfg!(windows) { "python" } else { "python3" })
                    .arg(path)
                    .current_dir(&self.cwd),
            )?;
        }
        Ok(())
    }
    fn save(&mut self, record: &Value) -> Result<(), String> {
        let mut record = record.clone();
        record["cwd"] = json!(self.cwd);
        record["review_dir"] = json!(self.reviews);
        let ledger = if record["ended"].is_number() {
            self.ledger_row(&record).and_then(|row| {
                crate::writer_ledger::append(
                    self.record_path
                        .parent()
                        .ok_or("record directory missing")?,
                    &row,
                )
            })
        } else {
            Ok(())
        };
        if let Err(error) = &ledger {
            record["ledger_error"] = json!(error);
        }
        atomic_write_private(&self.record_path, record.to_string().as_bytes())
            .map_err(|e| e.to_string())?;
        ledger
    }
}
pub(crate) fn find_review(directory: &Path, head: &str) -> Result<Option<Value>, String> {
    if head.len() < 8 {
        return Err("invalid final head".into());
    }
    let mut matches = Vec::new();
    for entry in fs::read_dir(directory).map_err(|e| e.to_string())? {
        let path = entry.map_err(|e| e.to_string())?.path();
        if path.extension().and_then(|s| s.to_str()) != Some("txt") {
            continue;
        }
        let meta = fs::symlink_metadata(&path).map_err(|e| e.to_string())?;
        if !meta.is_file() || meta.len() > 1024 * 1024 {
            continue;
        }
        let text = fs::read_to_string(&path).map_err(|e| e.to_string())?;
        let name = path.file_name().unwrap().to_string_lossy();
        if !name.contains(&head[..8]) && !text.lines().take(5).any(|l| l.contains(head)) {
            continue;
        }
        let verdict = text
            .lines()
            .rev()
            .find(|l| l.to_ascii_uppercase().contains("VERDICT"));
        if let Some(line) = verdict {
            let verdict = match line.trim() {
                "VERDICT: ACCEPT" => "ACCEPT",
                "VERDICT: REQUEST_CHANGES" | "VERDICT: REJECT" => "REQUEST_CHANGES",
                _ => continue,
            };
            matches.push((
                meta.modified().map_err(|e| e.to_string())?,
                json!({"path":path,"text":text,"verdict":verdict,"head":head}),
            ));
        }
    }
    matches.sort_by_key(|m| m.0);
    Ok(matches.pop().map(|m| m.1))
}
pub(crate) fn execute(args: Vec<String>) -> Result<(), String> {
    if cfg!(windows) {
        return Err("native writer requires the POSIX overflow transport in slice 1".into());
    }
    if args.len() != 1 {
        return Err("native-writer requires a private request file".into());
    }
    let request: Value = serde_json::from_slice(&fs::read(&args[0]).map_err(|e| e.to_string())?)
        .map_err(|e| e.to_string())?;
    let mut record = request["record"].clone();
    record["pid"] = json!(std::process::id());
    #[cfg(target_os = "linux")]
    if let Ok(stat) = fs::read_to_string("/proc/self/stat") {
        if let Some(ticks) = stat
            .rsplit_once(')')
            .and_then(|(_, tail)| tail.split_whitespace().nth(19))
            .and_then(|s| s.parse::<u64>().ok())
        {
            record["pid_start"] = json!(ticks);
        }
    }
    let repo = PathBuf::from(record["repo"].as_str().ok_or("repo missing")?);
    let id = record["run_id"]
        .as_str()
        .filter(|s| s.starts_with("run-") && s[4..].bytes().all(|b| b.is_ascii_alphanumeric()))
        .ok_or("invalid run id")?;
    let record_path = PathBuf::from(
        request["record_path"]
            .as_str()
            .ok_or("record_path missing")?,
    );
    let cwd = record_path
        .parent()
        .ok_or("record directory missing")?
        .join("worktree");
    let reviews = request["card"]["review_dir"]
        .as_str()
        .map(PathBuf::from)
        .unwrap_or_else(|| cwd.join("reviews"));
    let mut host = Host {
        repo,
        run_id: id.into(),
        cwd,
        reviews,
        record_path,
        base_ref: record["base_ref"].as_str().unwrap_or("").into(),
    };
    host.save(&record)?;
    native_writer::run(
        &mut host,
        record,
        request["prompt"].as_str().ok_or("prompt missing")?,
    );
    Ok(())
}
#[cfg(test)]
#[path = "native_writer_host_tests.rs"]
mod tests;
