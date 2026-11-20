//! Native CLI writer lifecycle, independent of placement and transport.
use serde_json::{json, Value};
pub(crate) trait Driver {
    fn prepare(&mut self) -> Result<String, String>;
    fn refresh_base(&mut self) -> Result<String, String>;
    fn disk_gb(&mut self) -> Result<u64, String>;
    fn execute(&mut self, session: Option<&str>, prompt: &str) -> Result<String, String>;
    fn head(&mut self) -> Result<String, String>;
    fn review(&mut self, head: &str) -> Result<Option<Value>, String>;
    fn gates(&mut self) -> Result<(), String>;
    fn save(&mut self, record: &Value) -> Result<(), String>;
}
pub(crate) fn run(driver: &mut impl Driver, mut record: Value, brief: &str) -> Value {
    record["manifest"] = json!({
        "name":record["run_id"], "host":record["host"], "worktree":record["cwd"],
        "brief":brief, "effort":"medium", "try_count":0
    });
    record["review_rounds"] = json!(0);
    let result = lifecycle(driver, &mut record, brief);
    let (mut exit, mut reason) = match result {
        Ok(code) => (code, None),
        Err((code, why)) => (code, Some(why)),
    };
    // Even a failed writer may have committed; retain the last observable head.
    if let Ok(head) = driver.head() {
        if exit == 0 && record["final_head"] != head {
            exit = 7;
            reason = Some("HEAD changed after review".into());
        }
        record["final_head"] = json!(head);
    }
    record["exit_code"] = json!(exit);
    record["status"] = json!(if exit == 0 { "completed" } else { "failed" });
    record["reason"] = json!(reason);
    if exit == 9 {
        record["result"] = json!("BLOCKED");
    }
    record["ended"] = json!(std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs_f64());
    if let Err(error) = driver.save(&record) {
        eprintln!("native writer record save failed: {error}");
    }
    record
}
fn admit_disk(driver: &mut impl Driver) -> Result<(), (i32, String)> {
    if driver.disk_gb().map_err(|e| (8, e))? < 5 {
        return Err((8, "disk_low: need 5 GB free to start".into()));
    }
    Ok(())
}
fn lifecycle(
    driver: &mut impl Driver,
    record: &mut Value,
    brief: &str,
) -> Result<i32, (i32, String)> {
    admit_disk(driver)?;
    record["base_sha"] = json!(driver.prepare().map_err(|e| (5, e))?);
    // Worktree creation can consume disk after placement admitted this host.
    admit_disk(driver)?;
    record["base_admitted"] = json!(true);
    driver.save(record).map_err(|e| (6, e))?;
    let prompt = format!("{brief}\nNative writer rules: new code is Rust with Rust tests. No new Python, shell or Node source files or additions to tools/new_code_allowlist.txt without an operator D-id; fixes inside existing Python files remain allowed until their port. Do not push. Before finishing, commit your work, run the repository's fast gates, and obtain a repository-access review for the final HEAD. Store the review in the configured review directory with the HEAD's eight-character SHA in its filename and a VERDICT: ACCEPT or VERDICT: REQUEST_CHANGES line. Review routing must use role review and writer_families [\"openai\"].\n");
    let prompt = format!("{prompt}\nUse the inherited shared CARGO_TARGET_DIR for every Cargo command, including reviews and preflight. Never override it with a private /tmp or worktree target, --target-dir, or Cargo configuration.\n");
    for attempt in 1..=3 {
        record["manifest"]["try_count"] = json!(attempt);
        driver.save(record).map_err(|e| (6, e))?;
        match writer_attempt(driver, record, &prompt) {
            Err((6, error)) => {
                // A review decision is terminal even if the CLI subsequently crashed.
                if let Ok(head) = driver.head() {
                    if let Ok(Some(review)) = driver.review(&head) {
                        if review["verdict"] == "REQUEST_CHANGES" {
                            record["review"] = review;
                            return Ok(3);
                        }
                    }
                }
                record["last_writer_failure"] = json!(error);
                driver.save(record).map_err(|e| (9, e))?;
                if attempt == 3 {
                    return Err((
                        9,
                        "writer failed three times; relaunch budget exhausted".into(),
                    ));
                }
                admit_disk(driver).map_err(|(_, e)| (9, e))?;
                record["base_sha"] = json!(driver.refresh_base().map_err(|e| (9, e))?);
                for key in ["session_id", "review", "final_head", "resumes"] {
                    record.as_object_mut().unwrap().remove(key);
                }
            }
            result => return result,
        }
    }
    unreachable!()
}
fn writer_attempt(
    driver: &mut impl Driver,
    record: &mut Value,
    prompt: &str,
) -> Result<i32, (i32, String)> {
    let session = driver.execute(None, prompt).map_err(|e| (6, e))?;
    record["session_id"] = json!(session);
    for attempt in 0..=3 {
        let head = driver.head().map_err(|e| (6, e))?;
        record["final_head"] = json!(head);
        record["resumes"] = json!(attempt);
        driver.save(record).map_err(|e| (6, e))?;
        if let Some(review) = driver.review(&head).map_err(|e| (4, e))? {
            record["review_rounds"] = json!(record["review_rounds"].as_u64().unwrap_or(0) + 1);
            let accepts = review["verdict"] == "ACCEPT";
            let weak = review["status"] == "no_verdict";
            record["review"] = review;
            if weak {
                return Err((4, "weak_verdict".into()));
            }
            if !accepts {
                return Ok(3);
            }
            driver.gates().map_err(|e| (7, e))?;
            if driver.head().map_err(|e| (7, e))? != head {
                return Err((7, "HEAD changed during final gates".into()));
            }
            return Ok(0);
        }
        if attempt == 3 || session.is_empty() {
            return Ok(4);
        }
        driver.execute(Some(&session), &format!("Your final head {head} has no required review. Run the mandatory repository-access review on that exact head, address findings and finish. Use the configured review directory; keep this session.")).map_err(|e| (6, e))?;
    }
    Ok(4)
}
#[cfg(test)]
#[path = "native_writer_tests.rs"]
mod tests;
