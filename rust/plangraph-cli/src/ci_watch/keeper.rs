use super::{logs, Result, Task};
use regex::Regex;
use serde_json::Value;
use std::collections::BTreeMap;
use std::time::Duration;

const PARENT: &str = "plangraph:PH-ORCH-SETUP";
trait Keeper {
    fn show(&mut self, key: &str) -> Result<Option<Value>>;
    fn create(&mut self, item: Value) -> Result<()>;
    /// Cancels an item (the fix task of a main that is green again).
    fn cancel(&mut self, _key: &str, _reason: &str) -> Result<()> {
        Err("cancel is not supported".into())
    }
}
struct Http {
    agent: ureq::Agent,
    base: String,
    token: String,
}
impl Keeper for Http {
    fn cancel(&mut self, key: &str, reason: &str) -> Result<()> {
        self.agent
            .post(format!("{}/v1/cancel?fmt=json", self.base))
            .header("Authorization", format!("Bearer {}", self.token))
            .send_json(serde_json::json!({"key": key, "reason": reason}))
            .map_err(|_| "keeper cancel refused; check token permissions".to_owned())?;
        Ok(())
    }
    fn show(&mut self, key: &str) -> Result<Option<Value>> {
        match self
            .agent
            .get(format!("{}/v1/show?fmt=json&key={key}", self.base))
            .header("Authorization", format!("Bearer {}", self.token))
            .call()
        {
            Ok(mut response) => response
                .body_mut()
                .read_json()
                .map(Some)
                .map_err(|_| "invalid keeper response".into()),
            Err(ureq::Error::StatusCode(404)) => Ok(None),
            Err(_) => Err("keeper lookup failed".into()),
        }
    }
    fn create(&mut self, item: Value) -> Result<()> {
        self.agent
            .post(format!("{}/v1/create_items?fmt=json", self.base))
            .header("Authorization", format!("Bearer {}", self.token))
            .send_json(serde_json::json!({"items":[item]}))
            .map_err(|_| {
                "keeper create_items refused; check token permissions and parent state".to_owned()
            })?;
        Ok(())
    }
}
/// Cancels the fix task `key` in the keeper (main is green again).
pub fn cancel(env: &BTreeMap<String, String>, key: &str) -> Result<()> {
    let mut http = http(env)?;
    // A task whose creation reply was lost may not exist: there is nothing to close then.
    if http.show(key)?.is_none() {
        return Ok(());
    }
    http.cancel(key, "main is green again")
}

fn http(env: &BTreeMap<String, String>) -> Result<Http> {
    let base = env.get("PG_URL").ok_or("PG_URL required")?;
    let token_path = env.get("PG_TOKEN_FILE").ok_or("PG_TOKEN_FILE required")?;
    let token = std::fs::read_to_string(token_path).map_err(|_| "cannot read keeper token file")?;
    let agent: ureq::Agent = ureq::Agent::config_builder()
        .proxy(None)
        .max_redirects(0)
        .timeout_global(Some(Duration::from_secs(30)))
        .build()
        .into();
    Ok(Http {
        agent,
        base: base.trim_end_matches('/').into(),
        token: token.trim().into(),
    })
}

pub fn define(env: &BTreeMap<String, String>, task: &mut Task) -> Result<bool> {
    let base = env.get("PG_URL").ok_or("PG_URL required")?;
    let token_path = env.get("PG_TOKEN_FILE").ok_or("PG_TOKEN_FILE required")?;
    let token = std::fs::read_to_string(token_path).map_err(|_| "cannot read keeper token file")?;
    let agent: ureq::Agent = ureq::Agent::config_builder()
        .proxy(None)
        .max_redirects(0)
        .timeout_global(Some(Duration::from_secs(30)))
        .build()
        .into();
    deliver(
        &mut Http {
            agent,
            base: base.trim_end_matches('/').into(),
            token: token.trim().into(),
        },
        task,
    )
}
fn terminal(item: &Value) -> Result<bool> {
    match item["state"].as_str() {
        Some("done" | "cancelled") => Ok(true),
        Some(_) => Ok(false),
        None => Err("keeper response missing state".into()),
    }
}
fn deliver(keeper: &mut impl Keeper, task: &mut Task) -> Result<bool> {
    let mut key = task.keeper_key.clone().unwrap_or_else(|| task.key.clone());
    let marker = format!("[ci-watch-run:{}]", task.run_id);
    if let Some(item) = keeper.show(&key)? {
        if !terminal(&item)?
            || item["description"]
                .as_str()
                .is_some_and(|d| d.contains(&marker))
        {
            task.keeper_key = Some(key);
            return Ok(true);
        }
        // A later regression gets a successor lane; the local head/test row
        // stays unique. The run suffix is deterministic across lost replies.
        key = format!("{}-R{}", task.key, task.run_id);
        if let Some(item) = keeper.show(&key)? {
            if terminal(&item)?
                && !item["description"]
                    .as_str()
                    .is_some_and(|d| d.contains(&marker))
            {
                return Err("keeper successor is terminal for a different failure".into());
            }
            task.keeper_key = Some(key);
            return Ok(true);
        }
    }
    let parent = keeper
        .show(PARENT)?
        .ok_or("keeper parent plangraph:PH-ORCH-SETUP is missing")?;
    if terminal(&parent)? {
        return Err("keeper parent plangraph:PH-ORCH-SETUP must be active".into());
    }
    keeper.create(definition(task, &key))?;
    task.keeper_key = Some(key);
    Ok(true)
}

// Match the keeper's Unicode character caps and credential guard.
// Masking extra path-like matches is safe; the complete ID and excerpt stay local.
fn contract_text(text: &str, cap: usize) -> String {
    let mut text: String = logs::scrub(text).chars().take(cap).collect();
    let word = Regex::new(r"(?i)credential|password|passwd|pwd|login").unwrap();
    let shape = Regex::new(r"\w+/\w{6,}").unwrap();
    loop {
        let words: Vec<_> = word
            .find_iter(&text)
            .map(|m| {
                (
                    text[..m.start()].chars().count(),
                    text[..m.end()].chars().count(),
                )
            })
            .collect();
        let matches: Vec<_> = shape
            .find_iter(&text)
            .filter(|m| {
                let start = text[..m.start()].chars().count();
                let end = text[..m.end()].chars().count();
                words
                    .iter()
                    .any(|(a, b)| *a <= end + 120 && *b + 120 >= start)
            })
            .map(|m| m.range())
            .collect();
        if matches.is_empty() {
            break;
        }
        for range in matches.into_iter().rev() {
            text.replace_range(range, "[REDACTED]");
        }
    }
    text.chars().take(cap).collect()
}
/// The class of the red job, where the fix starts and the failed job id (empty for a row older than the classifier).
fn class_lines(task: &Task) -> String {
    if task.class.is_empty() {
        return String::new();
    }
    let start = super::classify::RedClass::from_name(&task.class)
        .map_or("the log lines below", |class| class.start_at());
    format!(
        "Class: {} (run: {})\nStart at: {}\nFailed job id: {}\n",
        task.class, task.run_class, start, task.job_id
    )
}
fn definition(task: &Task, key: &str) -> Value {
    let description=format!("Why: CI tests failed on {} (PR {}), run {}.\nHow: Reproduce the failure, fix its cause, and verify the failing test and CI.\n[ci-watch-run:{}]\n{}Job: {}\nTest: {}\nLocal task: {}\n{}",task.head,task.pr.map(|n|n.to_string()).unwrap_or_else(||"none".into()),task.run_id,task.run_id,class_lines(task),contract_text(&task.job,100),contract_text(&task.test,300),task.key,task.excerpt);
    serde_json::json!({
        "project":"plangraph", "id":key.trim_start_matches("plangraph:"),
        "title":contract_text(&format!("Fix CI {} {}",task.head,task.test),200),
        "kind":"lane", "track":"T0", "owner":"unassigned", "estimate_h":1,
        "depends_on":[], "done_criteria":"Failing test passes and CI is green",
        "evidence_kinds":["commit+ci"], "operator":false,"priority":10,
        "under":PARENT, "source":format!("ci-watch:{}",task.run_id),
        "description":contract_text(&description,2000)
    })
}

#[cfg(test)]
mod tests;
