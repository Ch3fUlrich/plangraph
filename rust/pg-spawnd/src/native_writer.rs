//! Native CLI writer lifecycle, independent of placement and transport.
use crate::writer_control::STOPPED;
use serde_json::{json, Value};
#[path = "native_writer_stage.rs"]
mod stage;
#[path = "native_writer_timing.rs"]
mod timing;
/// Batch writer driver (R-batch-01..04, 06): one writer session, fast checks per head,
/// an other-family review run on the exact head, fixes in the same session.
pub(crate) trait Driver {
    fn prepare(&mut self) -> Result<String, String>;
    fn refresh_base(&mut self) -> Result<String, String>;
    fn disk_gb(&mut self) -> Result<u64, String>;
    fn execute(&mut self, session: Option<&str>, prompt: &str) -> Result<String, String>;
    fn head(&mut self) -> Result<String, String>;
    fn has_commits_beyond(&mut self, base: &str, head: &str) -> Result<bool, String>;
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
    /// Push the accepted head; returns the pushed ref, or None when no push is configured.
    fn push(&mut self, _head: &str) -> Result<Option<String>, String> {
        Ok(None)
    }
    fn save(&mut self, record: &Value) -> Result<(), String>;
    fn pending_steer(&self, _record: &Value) -> Result<Vec<crate::native_steer::Message>, String> {
        Ok(Vec::new())
    }
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
/// A refused push can trigger two fix turns, subject to the overall review cap.
const PUSH_FIXES: usize = 2;
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
    finish(driver, record, result)
}

pub(crate) fn run_resume(
    driver: &mut impl Driver,
    mut record: Value,
    session: &str,
    prompt: &str,
) -> Value {
    let result = writer_attempt(driver, &mut record, prompt, Some(session));
    finish(driver, record, result)
}

fn finish(
    driver: &mut impl Driver,
    mut record: Value,
    result: Result<i32, (i32, String)>,
) -> Value {
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
    record["status"] = json!(if reason.as_deref() == Some(STOPPED) {
        "stopped"
    } else if exit == 2 {
        "refused"
    } else if exit == 0 {
        "completed"
    } else {
        "failed"
    });
    record["reason"] = json!(reason);
    if exit == 0 && record["status_line"] == "DONE" && record["review"]["verdict"] == "ACCEPT" {
        record["review_test_status"] = json!(if record["review"]["tested"] == true {
            "reported"
        } else {
            "untested"
        });
    }
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
    let mut saved_session = if record["landing_resume"] == true {
        Some(
            record["session_id"]
                .as_str()
                .ok_or((5, "saved writer session unavailable".to_owned()))?
                .to_owned(),
        )
    } else {
        record["base_sha"] = json!(driver.prepare().map_err(|e| (5, e))?);
        None
    };
    // Worktree creation can consume disk after placement admitted this host.
    admit_disk(driver)?;
    record["base_admitted"] = json!(true);
    driver.save(record).map_err(|e| (6, e))?;
    let skill = driver.skill(crate::role_header::skill_name(brief)).ok();
    let prompt = crate::role_header::compose("codex", brief, skill.as_deref());
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
            None => match latest_push_failure(record) {
                Some(failure) => {
                    format!("{prompt}\nUnresolved push failure on this worktree:\n{failure}\n")
                }
                None => prompt.clone(),
            },
        };
        match writer_attempt(driver, record, &launch, saved_session.take().as_deref()) {
            Err((_, error)) if error == STOPPED => return Err((10, error)),
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
                record["base_sha"] = json!(driver
                    .refresh_base()
                    .map_err(|e| (if e == STOPPED { 10 } else { 9 }, e))?);
                for key in ["session_id", "final_head", "resumes"] {
                    record.as_object_mut().unwrap().remove(key);
                }
            }
            result => return result,
        }
    }
    unreachable!()
}
fn latest_push_failure(record: &Value) -> Option<&str> {
    let failure = record["push_failures"].as_array()?.last()?;
    (record["review"]["head"] == failure["head"])
        .then(|| failure["output"].as_str())
        .flatten()
}

fn round_cap(record: &Value) -> bool {
    record["review_rounds"].as_u64().unwrap_or(0) >= crate::round_progress::MAX_ROUNDS
}

fn push_failure(record: &mut Value, head: &str, failure: &str) -> usize {
    let history = record
        .as_object_mut()
        .unwrap()
        .entry("push_failures")
        .or_insert_with(|| json!([]))
        .as_array_mut()
        .unwrap();
    history.push(json!({
        "head":head, "output":failure, "at":chrono::Utc::now().to_rfc3339()
    }));
    history.len()
}

fn resume(
    driver: &mut impl Driver,
    record: &mut Value,
    session: &str,
    prompt: &str,
) -> Result<(), (i32, String)> {
    let messages = driver.pending_steer(record).map_err(|e| (7, e))?;
    let prompt = if messages.is_empty() {
        prompt.to_owned()
    } else {
        let steer = messages
            .iter()
            .map(|entry| entry.message.as_str())
            .collect::<Vec<_>>()
            .join("\n\n");
        format!("Parent steering messages for this run:\n\n{steer}\n\n{prompt}")
    };
    record["resumes"] = json!(record["resumes"].as_u64().unwrap_or(0) + 1);
    timing::execute_turn(driver, record, Some(session), &prompt)?;
    if !messages.is_empty() {
        crate::native_steer::delivered(record, &messages);
        driver.save(record).map_err(|e| (6, e))?;
    }
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
    for _ in 0..REVIEW_TRIES {
        match driver.review(head, previous) {
            Ok(review) if review["head"] == head && review["status"] == REVIEW_DEFERRED => {
                return Ok(review)
            }
            Ok(review)
                if review["head"] == head
                    && ["ACCEPT", "REQUEST_CHANGES"]
                        .contains(&review["verdict"].as_str().unwrap_or("")) =>
            {
                return Ok(review)
            }
            Ok(review) if review["status"] == "client_missing" => {
                return Err((
                    10,
                    review["reason"]
                        .as_str()
                        .unwrap_or("client_missing")
                        .to_owned(),
                ));
            }
            Ok(review) if review["status"] == "review_running" => {
                return Err((
                    4,
                    review["reason"]
                        .as_str()
                        .unwrap_or("review_running")
                        .to_owned(),
                ));
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
    saved_session: Option<&str>,
) -> Result<i32, (i32, String)> {
    let base = match record["base_sha"].as_str() {
        Some(base) => base.to_owned(),
        // Older resume receipts can lack the admitted SHA.
        None => driver.head().map_err(|e| (6, e))?,
    };
    let mut checked_head = record["fast_checks"]["head"]
        .as_str()
        .or_else(|| record["review"]["head"].as_str())
        .map(str::to_owned);
    let session = if let Some(session) = saved_session {
        resume(driver, record, session, prompt)?;
        record.as_object_mut().unwrap().remove("landing_resume");
        session.to_owned()
    } else {
        let session = timing::execute_turn(driver, record, None, prompt)?;
        record["session_id"] = json!(session);
        record["resumes"] = json!(0);
        session
    };
    let mut fixes = 0;
    let mut idle = 0;
    loop {
        let head = driver.head().map_err(|e| (6, e))?;
        record["final_head"] = json!(head);
        driver.save(record).map_err(|e| (6, e))?;
        // A first check can use commits already on the worktree at the start of the turn.
        // Fix turns still need a different head before repeating checks or review.
        if checked_head.as_deref() == Some(head.as_str())
            || !driver
                .has_commits_beyond(&base, &head)
                .map_err(|e| (7, e))?
        {
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
        checked_head = Some(head.clone());
        let started = stage::now();
        let checked = driver.fast_checks(&head);
        if matches!(&checked, Err(error) if error == STOPPED) {
            return Err((10, STOPPED.into()));
        }
        stage::record(record, "fast_checks", &head, started);
        if let Err(failure) = checked {
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
        let started = stage::now();
        let review = review_round(driver, &head, previous.as_ref());
        stage::record(record, "review", &head, started);
        let review = review?;
        if review["status"] == REVIEW_DEFERRED {
            final_gates(driver, &head)?;
            record["review_status"] = json!(REVIEW_DEFERRED);
            record["review_causes"] = review["causes"].clone();
            record["status_line"] = json!("DEFERRED");
            return Ok(0);
        }
        record["review_rounds"] = json!(record["review_rounds"].as_u64().unwrap_or(0) + 1);
        let summary = json!({"head":head, "run_id":review["run_id"], "verdict":review["verdict"],
            "host":review["host"], "leg":review["leg"], "harness":review["harness"]});
        record["review"] = review;
        if record["reviews"].as_array().is_none() {
            record["reviews"] = json!([]);
        }
        record["reviews"].as_array_mut().unwrap().push(summary);
        if record["review"]["verdict"] == "ACCEPT" {
            if !driver.pending_steer(record).map_err(|e| (7, e))?.is_empty() {
                if round_cap(record) || session.is_empty() {
                    return Err((7, "steer pending after the final review round".into()));
                }
                resume(driver, record, &session, "Apply the parent steer to this package, commit any required changes and end your turn.")?;
                if driver.head().map_err(|e| (7, e))? != head {
                    continue;
                }
            }
            final_gates(driver, &head)?;
            let started = stage::now();
            let pushed = driver.push(&head);
            if !matches!(pushed, Ok(None)) {
                stage::record(record, "push", &head, started);
            }
            match pushed {
                Ok(target) => {
                    record["pushed"] = json!(target);
                    record["status_line"] = json!("DONE");
                    return Ok(0);
                }
                Err(failure) => {
                    let failures = push_failure(record, &head, &failure);
                    if failures > PUSH_FIXES || round_cap(record) || session.is_empty() {
                        return Err((7, format!("push failed on {head}: {failure}")));
                    }
                    resume(driver, record, &session, &format!(
                        "The push of your accepted head {head} was refused:\n{failure}\nFix the cause, commit and end your turn; keep this session."
                    ))?;
                    if driver.head().map_err(|e| (7, e))? == head {
                        return Err((
                            7,
                            format!("no_commit: push fix left HEAD at {head}: {failure}"),
                        ));
                    }
                    continue;
                }
            }
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
        if let Some(reason) =
            round_stop(&counts).or_else(|| round_cap(record).then_some("round_cap"))
        {
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
