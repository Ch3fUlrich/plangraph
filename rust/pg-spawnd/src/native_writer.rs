//! Native CLI writer lifecycle, independent of placement and transport.
use serde_json::{json, Value};
/// Batch writer driver (R-batch-01..04, 06): one writer session, fast checks per head,
/// an other-family review run on the exact head, fixes in the same session.
pub(crate) trait Driver {
    fn prepare(&mut self) -> Result<String, String>;
    fn refresh_base(&mut self) -> Result<String, String>;
    fn disk_gb(&mut self) -> Result<u64, String>;
    fn execute(&mut self, session: Option<&str>, prompt: &str) -> Result<String, String>;
    fn head(&mut self) -> Result<String, String>;
    /// Run the fast checks on `head`; success writes that head's stamp.
    fn fast_checks(&mut self, head: &str) -> Result<(), String>;
    /// Whether the fast-checks stamp for exactly `head` exists.
    fn stamped(&mut self, head: &str) -> bool;
    /// Start one review run on `head` and return its structured verdict, read from the
    /// review run record: `verdict` (ACCEPT, REQUEST_CHANGES or no_verdict), `head`,
    /// `run_id`, `text`. `previous` is the last REQUEST_CHANGES review, for a delta review.
    fn review(&mut self, head: &str, previous: Option<&Value>) -> Result<Value, String>;
    /// Final gates on the accepted head, the commit-trailer gate included.
    fn gates(&mut self) -> Result<(), String>;
    /// Push the accepted head once; returns the pushed ref, or None when no push is configured.
    fn push(&mut self, _head: &str) -> Result<Option<String>, String> {
        Ok(None)
    }
    fn save(&mut self, record: &Value) -> Result<(), String>;
    /// Skill text from origin/main of the run checkout; absent when unreadable.
    fn skill(&mut self, _name: &str) -> Result<String, String> {
        Err("skill unavailable".into())
    }
}
/// Review attempts per round before the run ends BLOCKED for want of a verdict.
const REVIEW_TRIES: usize = 3;
const REVIEW_DEFERRED: &str = "deferred_to_batch_seat";
/// Fast-check fix turns per head before the run fails.
const FAST_CHECK_FIXES: usize = 2;
/// Resumes after turns that left no commit beyond the base before the run fails.
const NO_COMMIT_RESUMES: usize = 2;
/// Stop rule after a REQUEST_CHANGES round, given the confirmed HIGH+MED count of every
/// round so far: the round progress rule of `round_progress`, applied to the round just
/// finished. The one seam a later stop rule replaces.
pub(crate) fn round_stop(counts: &[u64]) -> Option<&'static str> {
    if counts.len() as u64 >= crate::round_progress::MAX_ROUNDS {
        return Some("round_cap");
    }
    crate::round_progress::stop_reason(counts, counts.len() as u64, false)
}
/// HIGH+MED (P1/P2) finding lines of a review text.
pub(crate) fn high_med(text: &str) -> u64 {
    text.lines()
        .filter(|line| crate::pre_review::finding_description(line).is_some())
        .count() as u64
}
pub(crate) fn run(driver: &mut impl Driver, mut record: Value, brief: &str) -> Value {
    record["manifest"] = json!({
        "name":record["run_id"], "host":record["host"], "worktree":record["cwd"],
        "brief":brief, "effort":record["effort"].as_str().unwrap_or("medium"), "try_count":0
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
    if matches!(exit, 4 | 9) {
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
    let skill = driver.skill(crate::role_header::skill_name(brief)).ok();
    let brief = crate::role_header::compose("codex", brief, skill.as_deref());
    let prompt = format!("{brief}\nNative writer rules: new code is Rust with Rust tests. No new Python, shell or Node source files or additions to tools/new_code_allowlist.txt without an operator D-id; fixes inside existing Python files remain allowed until their port. Do not push. Commit your work and end your turn: the driver runs the fast checks and an other-family review on your final HEAD and resumes this session with any failure or finding.\n");
    let prompt = format!("{prompt}\nUse the inherited shared CARGO_TARGET_DIR for every Cargo command, including reviews and preflight. Never override it with a private /tmp or worktree target, --target-dir, or Cargo configuration.\n");
    for attempt in 1..=3 {
        record["manifest"]["try_count"] = json!(attempt);
        driver.save(record).map_err(|e| (6, e))?;
        // A relaunched session inherits the open findings of the last review.
        let open = (record["review"]["verdict"] == "REQUEST_CHANGES")
            .then(|| record["review"]["text"].as_str().unwrap_or("").to_owned());
        let launch = match open {
            Some(text) => format!(
                "{prompt}\nOpen findings of the last review on this worktree; fix them:\n{text}\n"
            ),
            None => prompt.clone(),
        };
        match writer_attempt(driver, record, &launch) {
            Err((6, error)) => {
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
                for key in ["session_id", "final_head", "resumes"] {
                    record.as_object_mut().unwrap().remove(key);
                }
            }
            result => return result,
        }
    }
    unreachable!()
}
fn resume(
    driver: &mut impl Driver,
    record: &mut Value,
    session: &str,
    prompt: &str,
) -> Result<(), (i32, String)> {
    record["resumes"] = json!(record["resumes"].as_u64().unwrap_or(0) + 1);
    driver.save(record).map_err(|e| (6, e))?;
    driver.execute(Some(session), prompt).map_err(|e| (6, e))?;
    Ok(())
}
/// One review round on `head`: refused without the fast-checks stamp; a run without a
/// verdict on exactly `head` is retried, then the run ends BLOCKED (exit 4).
fn review_round(
    driver: &mut impl Driver,
    head: &str,
    previous: Option<&Value>,
) -> Result<Value, (i32, String)> {
    if !driver.stamped(head) {
        return Err((
            7,
            format!("review_refused: no fast-checks stamp for {head}"),
        ));
    }
    let mut last = String::from("no review run");
    for attempt in 0..REVIEW_TRIES {
        match driver.review(head, previous) {
            Ok(review)
                if attempt == 0
                    && previous.is_none()
                    && review["head"] == head
                    && review["status"] == REVIEW_DEFERRED =>
            {
                return Ok(review)
            }
            Ok(review)
                if review["head"] == head
                    && ["ACCEPT", "REQUEST_CHANGES"]
                        .contains(&review["verdict"].as_str().unwrap_or("")) =>
            {
                return Ok(review)
            }
            Ok(review) => {
                last = review["reason"]
                    .as_str()
                    .unwrap_or("weak_verdict")
                    .to_owned()
            }
            Err(error) => last = error,
        }
    }
    Err((
        4,
        format!("weak_verdict: no review run returned a verdict on {head}: {last}"),
    ))
}
fn final_gates(driver: &mut impl Driver, head: &str) -> Result<(), (i32, String)> {
    driver.gates().map_err(|e| (7, e))?;
    if driver.head().map_err(|e| (7, e))? != head {
        return Err((7, "HEAD changed during final gates".into()));
    }
    Ok(())
}
fn writer_attempt(
    driver: &mut impl Driver,
    record: &mut Value,
    prompt: &str,
) -> Result<i32, (i32, String)> {
    // HEAD before the turn: a relaunch keeps the commits of the earlier attempts.
    let mut turn_start = driver.head().map_err(|e| (6, e))?;
    let session = driver.execute(None, prompt).map_err(|e| (6, e))?;
    record["session_id"] = json!(session);
    record["resumes"] = json!(0);
    let mut fixes = 0;
    let mut idle = 0;
    loop {
        let head = driver.head().map_err(|e| (6, e))?;
        record["final_head"] = json!(head);
        driver.save(record).map_err(|e| (6, e))?;
        // No new commit since the turn began: the gates would rerun on the same head, or only
        // report an empty range. Send the writer back to its items instead of resuming it on
        // a failure it did not cause, or spending a review round on the same commit.
        if head == turn_start {
            idle += 1;
            if idle > NO_COMMIT_RESUMES || session.is_empty() {
                return Err((
                    7,
                    format!(
                        "no_commit: the writer ended its turn without a new commit after {head}"
                    ),
                ));
            }
            resume(driver, record, &session, &format!("No commit yet (HEAD is still {head}): continue with the items of your brief and the failures or findings above, commit your work and end your turn; keep this session."))?;
            continue;
        }
        idle = 0;
        turn_start = head.clone();
        if let Err(failure) = driver.fast_checks(&head) {
            record["fast_checks"] = json!({"head":head, "status":"failed", "output":failure});
            fixes += 1;
            if fixes > FAST_CHECK_FIXES || session.is_empty() {
                return Err((7, format!("fast checks failed on {head}: {failure}")));
            }
            resume(driver, record, &session, &format!("The fast checks failed on your head {head}:\n{failure}\nFix the cause, commit and end your turn; keep this session."))?;
            continue;
        }
        fixes = 0;
        record["fast_checks"] = json!({"head":head, "status":"passed"});
        let previous =
            (record["review"]["verdict"] == "REQUEST_CHANGES").then(|| record["review"].clone());
        let review = review_round(driver, &head, previous.as_ref())?;
        if review["status"] == REVIEW_DEFERRED {
            final_gates(driver, &head)?;
            record["review_status"] = json!(REVIEW_DEFERRED);
            record["review_causes"] = review["causes"].clone();
            record["status_line"] = json!("DEFERRED");
            return Ok(0);
        }
        record["review_rounds"] = json!(record["review_rounds"].as_u64().unwrap_or(0) + 1);
        let summary = json!({"head":head, "run_id":review["run_id"], "verdict":review["verdict"],
            "leg":review["leg"], "harness":review["harness"]});
        record["review"] = review;
        if record["reviews"].as_array().is_none() {
            record["reviews"] = json!([]);
        }
        record["reviews"].as_array_mut().unwrap().push(summary);
        if record["review"]["verdict"] == "ACCEPT" {
            final_gates(driver, &head)?;
            // One push per package: only the accepted, gated head leaves the host.
            record["pushed"] = json!(driver.push(&head).map_err(|e| (7, e))?);
            record["status_line"] = json!("DONE");
            return Ok(0);
        }
        let text = record["review"]["text"].as_str().unwrap_or("").to_owned();
        if record["round_counts"].as_array().is_none() {
            record["round_counts"] = json!([]);
        }
        record["round_counts"]
            .as_array_mut()
            .unwrap()
            .push(json!(high_med(&text)));
        let counts: Vec<u64> = record["round_counts"]
            .as_array()
            .unwrap()
            .iter()
            .filter_map(Value::as_u64)
            .collect();
        if let Some(reason) = round_stop(&counts) {
            record["round_stop"] = json!(reason);
            return Ok(3);
        }
        if session.is_empty() {
            record["round_stop"] = json!("no_session");
            return Ok(3);
        }
        resume(driver, record, &session, &format!("The review of your head {head} requests changes. Fix every confirmed HIGH and MED finding with a test that fails without the fix, or refute it with evidence in your final message; commit and end your turn; keep this session.\n----- review -----\n{text}\n----- end of review -----"))?;
    }
}
#[cfg(test)]
#[path = "native_writer_tests.rs"]
mod tests;
