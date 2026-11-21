use super::{printed_record, Host};
use crate::native_writer::REVIEW_CLEANUP_UNCONFIRMED;
use pg_worker_core::stall::{supervise_redirected, Opts};
use serde_json::Value;
use std::{
    io::{Read, Seek},
    path::Path,
    process::Stdio,
    thread,
    time::{Duration, Instant},
};

pub(super) struct ReviewCheckout {
    pub path: std::path::PathBuf,
    pub removable: bool,
}

impl Drop for ReviewCheckout {
    fn drop(&mut self) {
        if self.removable {
            let _ = std::fs::remove_dir_all(&self.path);
        }
    }
}

fn terminal(record: &Value) -> bool {
    crate::runs::terminal(record)
        || matches!(
            record["status"].as_str(),
            Some("succeeded" | "terminated_after_answer" | "skipped" | "killed")
        )
}

impl Host {
    pub(super) fn review_request(&self, clone: &Path, args: &[&str]) -> Result<Value, String> {
        let mut stdout = tempfile::tempfile().map_err(|e| e.to_string())?;
        let mut stderr = tempfile::tempfile().map_err(|e| e.to_string())?;
        let mut command = self.command(&self.review_cli[0]);
        command
            .args(&self.review_cli[1..])
            .arg("--state")
            .arg(&self.state)
            .args(args)
            .current_dir(clone)
            .stdin(Stdio::null())
            .stdout(stdout.try_clone().map_err(|e| e.to_string())?)
            .stderr(stderr.try_clone().map_err(|e| e.to_string())?);
        let seconds = match args.first() {
            Some(&"status") => 5,
            Some(&"cancel") => 15,
            _ => 60,
        };
        supervise_redirected(
            command,
            &Opts {
                timeout: Some(Duration::from_secs(seconds)),
                grace: Duration::from_secs(1),
                ..Opts::default()
            },
            || Ok(false),
        )
        .map_err(|e| e.to_string())?;
        let mut result = String::new();
        stdout.rewind().map_err(|e| e.to_string())?;
        stdout
            .read_to_string(&mut result)
            .map_err(|e| e.to_string())?;
        printed_record(&result).ok_or_else(|| {
            let mut error = String::new();
            let _ = stderr
                .rewind()
                .and_then(|()| stderr.read_to_string(&mut error));
            format!("review run returned no record: {}", error.trim())
        })
    }

    pub(super) fn finish_review(
        &self,
        checkout: &mut ReviewCheckout,
        mut record: Value,
    ) -> Result<Value, String> {
        let id = record["run_id"]
            .as_str()
            .filter(|id| pg_worker_core::run::record::valid_run_id(id))
            .ok_or("review returned invalid run id")?
            .to_owned();
        self.finish_review_in(checkout, &mut record, &id, Duration::from_secs(3300))?;
        Ok(record)
    }

    pub(super) fn finish_review_in(
        &self,
        checkout: &mut ReviewCheckout,
        record: &mut Value,
        id: &str,
        timeout: Duration,
    ) -> Result<(), String> {
        let start = Instant::now();
        let mut interrupted = None;
        let mut cancel_at = None;
        let mut status_error: Option<String> = None;
        while !terminal(record) {
            let reason = if self.cancelled() {
                Some("review cancelled")
            } else if start.elapsed() >= timeout {
                Some("review timed out")
            } else {
                status_error.as_deref()
            };
            let verb = if interrupted.is_none() && reason.is_some() {
                interrupted = reason.map(str::to_owned);
                cancel_at = Some(Instant::now());
                "cancel"
            } else {
                "status"
            };
            if cancel_at.is_some_and(|at| at.elapsed() >= Duration::from_secs(15)) {
                return Err(format!(
                    "{REVIEW_CLEANUP_UNCONFIRMED}: {id}; checkout retained"
                ));
            }
            match self.review_request(&checkout.path, &[verb, id]) {
                Ok(updated) if updated["run_id"] == id => *record = updated,
                response => {
                    let detail = response.err().unwrap_or_else(|| "unexpected run id".into());
                    if interrupted.is_some() {
                        return Err(format!(
                            "{REVIEW_CLEANUP_UNCONFIRMED}: {id}: {detail}; checkout retained"
                        ));
                    }
                    status_error = Some(detail);
                    continue;
                }
            }
            if !terminal(record) {
                thread::sleep(Duration::from_secs(1));
            }
        }
        if record["reason"] == "kill_incomplete"
            || record["survivors"]
                .as_array()
                .is_some_and(|pids| !pids.is_empty())
        {
            return Err(format!(
                "{REVIEW_CLEANUP_UNCONFIRMED}: {id}; checkout retained"
            ));
        }
        checkout.removable = true;
        interrupted.map_or(Ok(()), Err)
    }
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;
    use serde_json::json;
    use std::fs;

    #[test]
    fn review_completion_timeout_and_cancel_own_checkout_until_terminal() {
        let dir = tempfile::tempdir().unwrap();
        let cli = dir.path().join("fake-review");
        fs::write(
            &cli,
            r#"import json, pathlib, sys
verb, run = sys.argv[3:]
assert pathlib.Path("checkout-marker").is_file()
pathlib.Path("../request").write_text(verb)
status = "cancelled" if verb == "cancel" else "completed"
result = {"run_id": run, "status": status}
if pathlib.Path("leave-survivors").is_file():
    result["survivors"] = [123]
print(json.dumps(result))
"#,
        )
        .unwrap();
        let root = dir.path().to_owned();
        let host = Host {
            repo: root.clone(),
            run_id: "run-writer".into(),
            cwd: root.clone(),
            reviews: root.clone(),
            record_path: root.join("record.json"),
            base_ref: String::new(),
            tool_path: std::env::var("PATH").unwrap(),
            effort: String::new(),
            state: root.clone(),
            brief: String::new(),
            push_ref: None,
            reviews_started: 0,
            turn_timeout: Duration::from_secs(1),
            review_cli: vec!["python3".into(), cli.to_string_lossy().into_owned()],
            python_src: None,
        };
        for (mode, expected_verb, kept) in [
            ("exit", "status", false),
            ("timeout", "cancel", false),
            ("cancel", "cancel", false),
            ("kill-incomplete", "cancel", true),
        ] {
            let path = root.join(mode);
            fs::create_dir(&path).unwrap();
            fs::write(path.join("checkout-marker"), "").unwrap();
            if mode == "cancel" {
                fs::write(root.join("cancelled"), "").unwrap();
            }
            if kept {
                fs::write(path.join("leave-survivors"), "").unwrap();
            }
            let mut checkout = ReviewCheckout {
                path: path.clone(),
                removable: false,
            };
            let mut record = json!({"run_id":"run-review","status":"running"});
            let timeout = if expected_verb == "cancel" && mode != "cancel" {
                Duration::ZERO
            } else {
                Duration::from_secs(10)
            };
            let finished = host.finish_review_in(&mut checkout, &mut record, "run-review", timeout);
            assert_eq!(finished.is_ok(), mode == "exit", "{finished:?}");
            if kept {
                assert!(finished
                    .unwrap_err()
                    .starts_with(REVIEW_CLEANUP_UNCONFIRMED));
            }
            assert_eq!(
                fs::read_to_string(root.join("request")).unwrap(),
                expected_verb
            );
            drop(checkout);
            assert_eq!(path.exists(), kept);
            if mode == "cancel" {
                fs::remove_file(root.join("cancelled")).unwrap();
            }
        }
    }
}
