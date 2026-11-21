use super::{printed_record, Host};
use crate::native_writer::REVIEW_CLEANUP_UNCONFIRMED;
use pg_worker_core::stall::{supervise_redirected, Opts};
use serde_json::Value;
use std::{
    io::{self, Read, Seek},
    path::Path,
    process::Stdio,
    thread,
    time::{Duration, Instant},
};

// The spawner can spend 20 seconds waiting for its native cancellation marker
// before escalating. Allow that wait, escalation, and final status publication.
const CANCEL_REQUEST_TIMEOUT: Duration = Duration::from_secs(30);
const CANCEL_CONFIRM_TIMEOUT: Duration = Duration::from_secs(40);
const STATUS_ERROR_LIMIT: u8 = 3;

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

pub(super) struct ReviewRequestError {
    pub message: String,
    pub admission_unknown: bool,
}

impl From<io::Error> for ReviewRequestError {
    fn from(error: io::Error) -> Self {
        Self {
            message: error.to_string(),
            admission_unknown: false,
        }
    }
}

impl Host {
    pub(super) fn review_request(
        &self,
        clone: &Path,
        args: &[&str],
    ) -> Result<Value, ReviewRequestError> {
        let mut stdout = tempfile::tempfile()?;
        let mut stderr = tempfile::tempfile()?;
        let mut command = self.command(&self.review_cli[0]);
        command
            .args(&self.review_cli[1..])
            .arg("--state")
            .arg(&self.state)
            .args(args)
            .current_dir(clone)
            .stdin(Stdio::null())
            .stdout(stdout.try_clone()?)
            .stderr(stderr.try_clone()?);
        let timeout = match args.first() {
            Some(&"status") => Duration::from_secs(5),
            Some(&"cancel") => CANCEL_REQUEST_TIMEOUT,
            _ => Duration::from_secs(60),
        };
        let status = supervise_redirected(
            command,
            &Opts {
                timeout: Some(timeout),
                grace: Duration::from_secs(1),
                ..Opts::default()
            },
            || Ok(false),
        )
        .map_err(|error| ReviewRequestError {
            admission_unknown: !matches!(
                error.kind(),
                io::ErrorKind::NotFound | io::ErrorKind::PermissionDenied
            ),
            message: error.to_string(),
        })?;
        // A signal may interrupt the CLI after it has launched a review.
        let admission_unknown = status.success() || status.code().is_none();
        let mut result = String::new();
        stdout
            .rewind()
            .and_then(|()| stdout.read_to_string(&mut result))
            .map_err(|error| ReviewRequestError {
                message: error.to_string(),
                admission_unknown,
            })?;
        printed_record(&result).ok_or_else(|| {
            let mut error = String::new();
            let _ = stderr
                .rewind()
                .and_then(|()| stderr.read_to_string(&mut error));
            ReviewRequestError {
                message: format!("review run returned no record: {}", error.trim()),
                admission_unknown,
            }
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
            .ok_or_else(|| format!("{REVIEW_CLEANUP_UNCONFIRMED}: review returned invalid run id; checkout retained"))?
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
        let mut status_errors = 0;
        while !terminal(record) {
            let reason = if self.cancelled() {
                Some("review cancelled")
            } else if start.elapsed() >= timeout {
                Some("review timed out")
            } else {
                None
            };
            let verb = if interrupted.is_none() && reason.is_some() {
                interrupted = reason;
                cancel_at = Some(Instant::now());
                "cancel"
            } else {
                "status"
            };
            if cancel_at.is_some_and(|at| at.elapsed() >= CANCEL_CONFIRM_TIMEOUT) {
                return Err(format!(
                    "{REVIEW_CLEANUP_UNCONFIRMED}: {id}; checkout retained"
                ));
            }
            match self.review_request(&checkout.path, &[verb, id]) {
                Ok(updated) if updated["run_id"] == id => {
                    *record = updated;
                    status_errors = 0;
                }
                response if verb == "status" => {
                    status_errors += 1;
                    if status_errors >= STATUS_ERROR_LIMIT {
                        let detail = response
                            .err()
                            .map_or_else(|| "unexpected run id".into(), |error| error.message);
                        return Err(format!(
                            "{REVIEW_CLEANUP_UNCONFIRMED}: {id}: {detail}; checkout retained"
                        ));
                    }
                }
                // A lost cancel reply can still be followed by a terminal status.
                _ => {}
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
        interrupted.map_or(Ok(()), |reason| Err(reason.into()))
    }
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;
    use serde_json::json;
    use std::fs;

    #[test]
    fn review_completion_timeout_and_cancel_own_checkout_until_terminal() {
        for (mode, requests, kept, error) in [
            ("exit", "status", false, None),
            ("timeout", "cancel", false, Some("review timed out")),
            ("cancel", "cancel", false, Some("review cancelled")),
            (
                "kill-incomplete",
                "cancel",
                true,
                Some(REVIEW_CLEANUP_UNCONFIRMED),
            ),
        ] {
            checkout_case(mode, requests, kept, error);
        }
    }

    #[test]
    fn status_errors_are_retried_without_cancelling() {
        checkout_case("transient", "status status", false, None);
        checkout_case(
            "unreadable",
            "status status status",
            true,
            Some(REVIEW_CLEANUP_UNCONFIRMED),
        );
    }

    #[test]
    fn cancellation_waits_past_the_spawners_twenty_second_grace() {
        checkout_case("slow-cancel", "cancel", false, Some("review timed out"));
    }

    fn checkout_case(mode: &str, expected_requests: &str, kept: bool, error: Option<&str>) {
        let dir = tempfile::tempdir().unwrap();
        let cli = dir.path().join("fake-review");
        fs::write(
            &cli,
            r#"import json, pathlib, sys, time
verb, run = sys.argv[3:]
assert pathlib.Path("checkout-marker").is_file()
requests = pathlib.Path("../requests")
with requests.open("a") as log:
    print(verb, file=log)
mode = pathlib.Path.cwd().name
if verb == "status" and (mode == "unreadable" or
                        (mode == "transient" and requests.read_text() == "status\n")):
    raise SystemExit("record temporarily unreadable")
if verb == "cancel" and mode == "slow-cancel":
    time.sleep(20.2)
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
        let path = root.join(mode);
        fs::create_dir(&path).unwrap();
        fs::write(path.join("checkout-marker"), "").unwrap();
        if mode == "cancel" {
            fs::write(root.join("cancelled"), "").unwrap();
        }
        if mode == "kill-incomplete" {
            fs::write(path.join("leave-survivors"), "").unwrap();
        }
        let mut checkout = ReviewCheckout {
            path: path.clone(),
            removable: false,
        };
        let mut record = json!({"run_id":"run-review","status":"running"});
        let timeout = if matches!(mode, "timeout" | "kill-incomplete" | "slow-cancel") {
            Duration::ZERO
        } else {
            Duration::from_secs(10)
        };
        let finished = host.finish_review_in(&mut checkout, &mut record, "run-review", timeout);
        match error {
            Some(prefix) => assert!(finished.unwrap_err().starts_with(prefix)),
            None => assert!(finished.is_ok(), "{finished:?}"),
        }
        let requests = fs::read_to_string(root.join("requests")).unwrap();
        assert_eq!(
            requests.split_whitespace().collect::<Vec<_>>().join(" "),
            expected_requests
        );
        drop(checkout);
        assert_eq!(path.exists(), kept);
    }
}
