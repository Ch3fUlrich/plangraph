//! Target-host execution for the placed native-Codex run kind.
use crate::native_writer::{self, Driver};
use pg_worker_core::run::record_io::atomic_write_private;
use serde_json::{json, Value};
use std::{
    fs,
    io::{BufRead, BufReader, Seek, SeekFrom, Write},
    path::{Path, PathBuf},
    process::{Command, Stdio},
    time::Duration,
};

/// Lead writer leg cx-gpt-6.2-sol-medium (operator D-6397).
const WRITER_MODEL: &str = "gpt-6.2-sol";
const WRITER_EFFORT: &str = "medium";
/// Writer effort: medium unless the card names `effort` high or xhigh together with
/// an `effort_decision` D-id that authorises it.
pub(crate) fn writer_effort(card: &Value) -> Result<String, String> {
    let Some(effort) = card.get("effort").filter(|v| !v.is_null()) else {
        return Ok(WRITER_EFFORT.into());
    };
    let effort = effort
        .as_str()
        .filter(|e| ["medium", "high", "xhigh"].contains(e))
        .ok_or("effort_invalid: card.effort must be medium, high or xhigh")?;
    let decided = card["effort_decision"].as_str().is_some_and(|d| {
        d.strip_prefix("D-")
            .is_some_and(|n| !n.is_empty() && n.bytes().all(|b| b.is_ascii_digit()))
    });
    if effort != WRITER_EFFORT && !decided {
        return Err(format!(
            "effort_needs_decision: card.effort {effort} requires card.effort_decision D-<n>"
        ));
    }
    Ok(effort.into())
}
/// Model and effort flags for the codex invocation; the ledger row records the same values.
fn model_args(effort: &str) -> [String; 6] {
    [
        "-m".into(),
        WRITER_MODEL.into(),
        "-c".into(),
        format!("model_reasoning_effort={effort}"),
        "-c".into(),
        "model_context_window=1000000".into(),
    ]
}

struct Host {
    repo: PathBuf,
    run_id: String,
    cwd: PathBuf,
    reviews: PathBuf,
    record_path: PathBuf,
    base_ref: String,
    tool_path: String,
    effort: String,
    /// Run state root holding `runs/<run-id>/`; review runs are recorded there too.
    state: PathBuf,
    /// Writer brief, repeated in every review prompt for its item keys and Done-when lines.
    brief: String,
    /// `refs/heads/...` that receives the accepted head; None keeps the head on the host.
    push_ref: Option<String>,
    reviews_started: usize,
    /// Spawner CLI argv prefix that starts review runs (`review_cli`).
    review_cli: Vec<String>,
    /// Checkout of trusted plangraph sources for the family table; None uses the installed
    /// package. Never the writer's worktree: the change under review must not pick the
    /// families it is reviewed against.
    python_src: Option<PathBuf>,
}
fn output(command: &mut Command) -> Result<String, String> {
    let result = command.output().map_err(|e| e.to_string())?;
    if !result.status.success() {
        // A tool may report on stdout only; the failure carries both streams in full.
        return Err(format!(
            "command failed ({}):\nstdout: {}\nstderr: {}",
            result.status,
            String::from_utf8_lossy(&result.stdout).trim(),
            String::from_utf8_lossy(&result.stderr).trim()
        ));
    }
    Ok(String::from_utf8_lossy(&result.stdout).trim().into())
}
impl Host {
    fn command(&self, program: impl AsRef<std::ffi::OsStr>) -> Command {
        let mut command = Command::new(program);
        command.env("PATH", &self.tool_path);
        command
    }
    fn git(&self, args: &[&str]) -> Result<String, String> {
        output(self.command("git").arg("-C").arg(&self.cwd).args(args))
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
            "host":record["host"],"model":WRITER_MODEL,"effort":self.effort,
            "started":record["started"],"ended":record["ended"],"exit_code":record["exit_code"],
            "item_key":record["item_key"],"items_given":item.map(|_| 1),"items_done":done,
            "changed_lines":lines,"wall_minutes":wall,"tokens":crate::writer_ledger::tokens(&log)?,
            "review_rounds":record["review_rounds"],
            "high_held":accepted.then_some(0),"med_held":accepted.then_some(0)}),
        )
    }
    fn base_pin(&self) -> String {
        format!("refs/plangraph/native/{}/base", self.run_id)
    }
    fn free(&self) -> Result<u64, String> {
        // State and repository may be on different filesystems. Probe the worktree's
        // nearest existing parent before creation, then the worktree itself.
        let mut path = self.cwd.as_path();
        while !path.exists() {
            path = path.parent().ok_or("worktree filesystem unavailable")?;
        }
        let text = output(self.command("df").args(["-Pk", "--"]).arg(path))?;
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
            return Err("base_ref must name a refs/heads/ or refs/tags/ ref".into());
        }
        output(
            self.command("git")
                .arg("-C")
                .arg(&self.repo)
                .args(["check-ref-format", &self.base_ref]),
        )?;
        // Pin the base in a per-run namespace: parallel fetches cannot change this base.
        let pin = format!("refs/plangraph/native/{}/base", self.run_id);
        self.pin_base(&pin)?;
        let base = output(
            self.command("git")
                .arg("-C")
                .arg(&self.repo)
                .args(["rev-parse", &format!("{pin}^{{commit}}")]),
        )?;
        output(self.command("git").arg("-C").arg(&self.repo).args([
            "merge-base",
            "--is-ancestor",
            "refs/remotes/origin/main",
            &base,
        ]))
        .map_err(|_| "stale base: pushed ref does not contain origin/main".to_owned())?;
        fs::create_dir_all(self.cwd.parent().ok_or("worktree parent missing")?)
            .map_err(|e| e.to_string())?;
        output(
            self.command("git")
                .arg("-C")
                .arg(&self.repo)
                .args(["worktree", "add", "--detach"])
                .arg(&self.cwd)
                .arg(&base),
        )?;
        fs::create_dir_all(&self.reviews).map_err(|e| e.to_string())?;
        Ok(base)
    }
    fn refresh_base(&mut self) -> Result<String, String> {
        self.git(&[
            "fetch",
            "origin",
            "+refs/heads/main:refs/remotes/origin/main",
        ])?;
        // Preserve writer commits and dirty files: conflicts fail closed for the orchestrator.
        self.git(&["merge", "--no-edit", "origin/main"])?;
        self.git(&["rev-parse", "origin/main"])
    }
    fn execute(&mut self, session: Option<&str>, prompt: &str) -> Result<String, String> {
        // The caller PATH recorded at admission selects Codex for every launch and resume.
        let codex = crate::native_writer_tool::resolve(self.tool_path.as_ref(), &self.cwd)?;
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
        let log_start = log.metadata().map_err(|e| e.to_string())?.len();
        let mut command = self.command(codex);
        command.arg("exec");
        if let Some(id) = session {
            command.args(["resume", id]);
        }
        command
            .env(
                "CARGO_TARGET_DIR",
                pg_worker_core::cargo_cache::shared_cache_roots()
                    .map_err(|e| e.to_string())?
                    .remove(0),
            )
            .env("CARGO_INCREMENTAL", "0")
            .args(["--json"])
            .args(model_args(&self.effort))
            .args([
                "-c",
                "approval_policy=never",
                "-c",
                &format!(
                    "sandbox_mode=\"{}\"",
                    pg_worker_core::host_readiness::WRITER_SANDBOX
                ),
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
            let mut log = fs::File::open(&log_path).map_err(|e| e.to_string())?;
            log.seek(SeekFrom::Start(log_start))
                .map_err(|e| e.to_string())?;
            let mut completed = false;
            let mut thread_id = session.map(str::to_owned);
            for line in BufReader::new(log).lines() {
                let line = line.map_err(|e| e.to_string())?;
                let Ok(event) = serde_json::from_str::<Value>(&line) else {
                    continue;
                };
                if event["type"] == "turn.completed" {
                    completed = true;
                }
                if event["type"] == "thread.started" {
                    thread_id = event["thread_id"].as_str().map(str::to_owned);
                }
            }
            if !completed {
                return Err("codex ended without a result line".into());
            }
            thread_id.ok_or_else(|| "codex did not report a session id".into())
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
    fn skill(&mut self, name: &str) -> Result<String, String> {
        let shown = self
            .command("git")
            .arg("-C")
            .arg(&self.cwd)
            .args(["show", &format!("origin/main:skills/{name}/SKILL.md")])
            .output()
            .map_err(|e| e.to_string())?;
        if shown.status.success() {
            return String::from_utf8(shown.stdout).map_err(|e| e.to_string());
        }
        // No origin/main in the checkout: the working-tree copy is the same file.
        std::fs::read_to_string(
            std::path::Path::new(&self.cwd).join(format!("skills/{name}/SKILL.md")),
        )
        .map_err(|e| format!("skill unreadable: {e}"))
    }
    fn head(&mut self) -> Result<String, String> {
        self.git(&["rev-parse", "HEAD"])
    }
    fn fast_checks(&mut self, head: &str) -> Result<(), String> {
        if self.git(&["rev-parse", "HEAD"])? != head {
            return Err(format!("worktree is not at {head}"));
        }
        let stamp = self.stamp(head)?;
        fs::create_dir_all(stamp.parent().ok_or("stamp directory missing")?)
            .map_err(|e| e.to_string())?;
        // The turns up to this head are bound to it before the gates run, so a head whose gate
        // fails keeps its own output and the next head starts after it.
        self.session_excerpt(&stamp)?;
        let log = self.checks()?;
        // The exact-head gate output, named in the review brief next to the stamp.
        fs::write(stamp.with_extension("log"), log).map_err(|e| e.to_string())?;
        fs::write(&stamp, format!("{}\n", chrono::Utc::now().to_rfc3339()))
            .map_err(|e| e.to_string())
    }
    fn stamped(&mut self, head: &str) -> bool {
        self.stamp(head).is_ok_and(|stamp| stamp.is_file())
    }
    fn review(&mut self, head: &str, previous: Option<&Value>) -> Result<Value, String> {
        self.review_run(head, previous)
    }
    fn gates(&mut self) -> Result<(), String> {
        self.git(&["diff", "--exit-code", "HEAD", "--"])?;
        self.checks().map(|_| ())
    }
    fn push(&mut self, head: &str) -> Result<Option<String>, String> {
        let Some(target) = self.push_ref.clone() else {
            return Ok(None);
        };
        // No force flag: the remote refuses anything but a fast-forward.
        self.git(&["push", "origin", &format!("{head}:{target}")])?;
        Ok(Some(target))
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
/// State root of a run whose record lives at `<state>/runs/<run-id>/record.json`.
fn record_path_state(request: &Value) -> Result<PathBuf, String> {
    Path::new(
        request["record_path"]
            .as_str()
            .ok_or("record_path missing")?,
    )
    .ancestors()
    .nth(3)
    .map(Path::to_path_buf)
    .ok_or_else(|| "record_path has no state root".into())
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
        tool_path: record["tool_path"]
            .as_str()
            .ok_or("tool PATH missing")?
            .into(),
        effort: WRITER_EFFORT.into(),
        state: record_path_state(&request)?,
        brief: request["prompt"].as_str().unwrap_or("").into(),
        push_ref: request["card"]["push_ref"].as_str().map(str::to_owned),
        reviews_started: 0,
        review_cli: review_cli()?,
        python_src: None,
    };
    if let Some(target) = &host.push_ref {
        if !target.starts_with("refs/heads/") {
            record["status"] = json!("failed");
            record["exit_code"] = json!(5);
            record["reason"] = json!("push_ref must name refs/heads/...");
            return host.save(&record);
        }
    }
    match writer_effort(&request["card"]) {
        Ok(effort) => host.effort = effort,
        Err(reason) => {
            // Refused before Codex starts: the record names the reason.
            record["status"] = json!("failed");
            record["exit_code"] = json!(5);
            record["reason"] = json!(reason);
            return host.save(&record);
        }
    }
    record["effort"] = json!(host.effort);
    record["cwd"] = json!(host.cwd);
    host.save(&record)?;
    native_writer::run(
        &mut host,
        record,
        request["prompt"].as_str().ok_or("prompt missing")?,
    );
    Ok(())
}
#[path = "native_writer_context.rs"]
mod context;
#[path = "native_writer_base.rs"]
mod base;
#[path = "native_writer_review.rs"]
mod review;
use review::review_cli;
#[cfg(test)]
use review::{review_run_text, review_verdict};
#[cfg(test)]
#[path = "native_writer_base_tests.rs"]
mod base_tests;
#[cfg(test)]
#[path = "native_writer_host_tests.rs"]
mod tests;
