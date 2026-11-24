//! Target-host execution for the placed native-Codex run kind.
use crate::native_writer::{self, Driver};
use pg_worker_core::run::lifecycle;
use serde_json::{json, Value};
use std::path::Path;
use std::{
    fs,
    io::{Read, Seek, SeekFrom},
    path::PathBuf,
    process::Command,
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
    /// Writer brief used to supply item keys, criteria and invariants to the reviewer.
    brief: String,
    /// `refs/heads/...` that receives the accepted head; None keeps the head on the host.
    push_ref: Option<String>,
    reviews_started: usize,
    review_override: Option<(String, String)>,
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
        #[cfg(unix)]
        {
            use std::os::unix::process::CommandExt;

            // Apply the run's mask in the child so concurrent daemon work keeps its own mask.
            // SAFETY: umask is async-signal-safe and the closure touches no shared Rust state.
            unsafe {
                command.pre_exec(|| {
                    libc::umask(0o022);
                    Ok(())
                });
            }
        }
        command
    }
    fn git(&self, args: &[&str]) -> Result<String, String> {
        output(self.command("git").arg("-C").arg(&self.cwd).args(args))
    }
    /// The Codex command line and environment of one writer turn: `workspace-write` with network
    /// access and the named writable roots, and only the allowlisted environment.
    fn writer_command(
        &self,
        codex: impl AsRef<std::ffi::OsStr>,
        session: Option<&str>,
        directory: &Path,
    ) -> Result<Command, String> {
        let cargo_target = pg_worker_core::cargo_cache::shared_cache_roots()
            .map_err(|e| e.to_string())?
            .remove(0);
        let git_dirs = crate::native_writer_sandbox::git_dirs(&self.cwd, Some(&self.tool_path))
            .map_err(|e| e.to_string())?;
        let roots = crate::native_writer_sandbox::writable_roots(
            &cargo_target,
            &self.reviews,
            directory,
            &git_dirs,
        );
        let mut command = self.command(codex);
        crate::native_writer_sandbox::apply_env(&mut command, std::env::vars_os(), &self.tool_path);
        #[cfg(target_os = "linux")]
        {
            // `cargo` resolves to a link to this executable: one shared lease per Cargo command.
            let exe = std::env::current_exe().map_err(|e| e.to_string())?;
            let shim = pg_worker_core::cargo_command_lease::install_shim(
                &directory.join("cargo-shim"),
                &exe,
            )
            .map_err(|e| format!("cargo shim: {e}"))?;
            let dir = shim.parent().unwrap_or(directory);
            command.env("PATH", format!("{}:{}", dir.display(), self.tool_path));
        }
        // Exec-level flags apply to fresh and resumed turns, including saved sessions
        // whose sandbox permissions were built from an earlier invocation.
        command
            .arg("exec")
            .arg("--cd")
            .arg(&self.cwd)
            .arg("--add-dir")
            .arg(&git_dirs[1]);
        if let Some(id) = session {
            command.args(["resume", id]);
        }
        command
            .env("CARGO_TARGET_DIR", &cargo_target)
            .env("CARGO_INCREMENTAL", "0")
            .args(["--json"])
            .args(model_args(&self.effort))
            .args(["-c", "approval_policy=never"])
            .args(crate::native_writer_sandbox::sandbox_args(&roots).map_err(|e| e.to_string())?)
            .arg("-o")
            .arg(directory.join("native.last"))
            .arg("-")
            .current_dir(&self.cwd);
        Ok(command)
    }
    fn ledger_row(&self, record: &Value) -> Result<Value, String> {
        let directory = self
            .record_path
            .parent()
            .ok_or("record directory missing")?;
        let mut log = String::new();
        match fs::File::open(directory.join("native.log")) {
            Ok(mut file) => {
                let offset = record["resume_from"]["log_offset"].as_u64().unwrap_or(0);
                file.seek(SeekFrom::Start(offset))
                    .map_err(|e| e.to_string())?;
                file.read_to_string(&mut log).map_err(|e| e.to_string())?;
            }
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
            Err(e) => return Err(e.to_string()),
        }
        let item = record["item_key"].as_str();
        let mut lines = Value::Null;
        let mut done = item.map(|_| 0);
        if let (Some(base), Some(head)) = (
            record["resume_from"]["head"]
                .as_str()
                .or_else(|| record["base_sha"].as_str()),
            record["final_head"].as_str(),
        ) {
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
        let mut row = json!({"schema_version":1,"kind":"writer","run_id":record["run_id"],
            "host":record["host"],"model":WRITER_MODEL,"effort":self.effort,
            "started":record["started"],"ended":record["ended"],"exit_code":record["exit_code"],
            "item_key":record["item_key"],"items_given":item.map(|_| 1),"items_done":done,
            "changed_lines":lines,"wall_minutes":wall,"tokens":crate::writer_ledger::tokens(&log)?,
            "review_rounds":record["review_rounds"],
            "high_held":accepted.then_some(0),"med_held":accepted.then_some(0)});
        if let Some(number) = record["resume_number"].as_u64() {
            row["resume_number"] = json!(number);
        }
        Ok(row)
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
        {
            let _creation =
                pg_worker_core::worktree::mutation_lock(&self.repo).map_err(|e| e.to_string())?;
            output(
                self.command("git")
                    .arg("-C")
                    .arg(&self.repo)
                    .args(["worktree", "add", "--detach"])
                    .arg(&self.cwd)
                    .arg(&base),
            )?;
        }
        fs::create_dir_all(&self.reviews).map_err(|e| e.to_string())?;
        Ok(base)
    }
    fn refresh_base(&mut self) -> Result<String, String> {
        let _guard = crate::writer_control::mutation_guard(&self.record_path)?;
        self.git(&[
            "fetch",
            "origin",
            "+refs/heads/main:refs/remotes/origin/main",
        ])?;
        // Preserve writer commits and dirty files: conflicts fail closed for the orchestrator.
        self.git(&["merge", "--no-edit", "origin/main"])?;
        self.git(&["rev-parse", "origin/main"])
    }
    fn execute(&mut self, session_id: Option<&str>, prompt: &str) -> Result<String, String> {
        session::execute(self, session_id, prompt)
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
    fn has_commits_beyond(&mut self, base: &str, head: &str) -> Result<bool, String> {
        self.git(&[
            "rev-list",
            "--max-count=1",
            "--end-of-options",
            &format!("{base}..{head}"),
            "--",
        ])
        .map(|commits| !commits.is_empty())
    }
    fn fast_checks(&mut self, head: &str) -> Result<(), String> {
        let _guard = crate::writer_control::mutation_guard(&self.record_path)?;
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
        let _guard = crate::writer_control::mutation_guard(&self.record_path)?;
        self.git(&["diff", "--exit-code", "HEAD", "--"])?;
        self.checks().map(|_| ())
    }
    #[cfg(target_os = "linux")]
    fn reclaim_target(&mut self) -> Result<(), String> {
        if self.push_ref.is_none() {
            return Ok(());
        }
        // The supervisor runs on the host outside the writer sandbox: the real Git directory of the
        // target is visible here, and no per-command lease of the writer is held any more.
        let _guard = crate::writer_control::mutation_guard(&self.record_path)?;
        let target = pg_worker_core::cargo_cache::shared_cache_roots()
            .map_err(|e| e.to_string())?
            .remove(0);
        pg_worker_core::disk_runtime::reclaim_before_push(
            &self.cwd,
            &target,
            pg_worker_core::cargo_pool::configured_cap_bytes(),
        )
        .map(|_| ())
    }
    fn push(&mut self, head: &str) -> Result<Option<String>, String> {
        let Some(target) = self.push_ref.clone() else {
            return Ok(None);
        };
        let _guard = crate::writer_control::mutation_guard(&self.record_path)?;
        // No force flag: the remote refuses anything but a fast-forward.
        self.git(&["push", "origin", &format!("{head}:{target}")])?;
        Ok(Some(target))
    }
    fn pending_steer(&self, record: &Value) -> Result<Vec<crate::native_steer::Message>, String> {
        let directory = self
            .record_path
            .parent()
            .ok_or("record directory missing")?;
        crate::native_steer::pending(directory, record)
    }
    fn save(&mut self, record: &Value) -> Result<(), String> {
        let mut record = record.clone();
        record["cwd"] = json!(self.cwd);
        record["review_dir"] = json!(self.reviews);
        for (field, file) in [
            ("writer_status_path", "status.txt"),
            ("writer_slices_path", "slices.json"),
        ] {
            if record[field].is_null() {
                record[field] = json!(self.reviews.join(file));
            }
        }
        if record["plan"]["effort"].is_object() {
            record["attempt_capture"] = json!(1);
        }
        lifecycle::publish(&self.state, &self.record_path, &record).map_err(|e| e.to_string())?;
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
            lifecycle::publish(&self.state, &self.record_path, &record)
                .map_err(|e| e.to_string())?;
        }
        ledger
    }
}
#[path = "native_writer_commands.rs"]
mod commands;
pub(crate) use commands::{execute, resume, resume_saved};
#[path = "native_writer_base.rs"]
mod base;
#[path = "native_writer_context.rs"]
mod context;
#[path = "native_writer_review.rs"]
mod review;
#[path = "native_writer_review_route.rs"]
mod review_route;
#[path = "native_writer_session.rs"]
mod session;
#[cfg(test)]
use review::review_diff_base;
pub(crate) use review::review_record_verdict;
#[cfg(test)]
use review::{review_run_text, review_verdict};
#[cfg(test)]
#[path = "native_writer_base_tests.rs"]
mod base_tests;
#[cfg(test)]
#[path = "native_writer_host_review_tests.rs"]
mod review_tests;
#[cfg(test)]
#[path = "native_writer_host_tests.rs"]
mod tests;

#[cfg(test)]
#[path = "native_writer_host_sandbox_tests.rs"]
mod sandbox_tests;
