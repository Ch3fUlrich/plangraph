//! Fast-check failures caused only by the build infrastructure rerun the gate on the same head.
use super::*;
use serde_json::json;

/// Writer turns up to `commit_turns` each commit `head-<turn>`; fast checks answer from
/// `checks` in order, then pass.
struct Fake {
    turns: usize,
    commit_turns: usize,
    prompts: Vec<String>,
    checks: Vec<&'static str>,
    checked: Vec<String>,
    stamped: Vec<String>,
    waits: Vec<u32>,
}
fn fake(checks: Vec<&'static str>) -> Fake {
    Fake {
        turns: 0,
        commit_turns: usize::MAX,
        prompts: vec![],
        checks,
        checked: vec![],
        stamped: vec![],
        waits: vec![],
    }
}
impl Driver for Fake {
    fn prepare(&mut self) -> Result<String, String> {
        Ok("base".into())
    }
    fn refresh_base(&mut self) -> Result<String, String> {
        Ok("base".into())
    }
    fn disk_gb(&mut self) -> Result<u64, String> {
        Ok(50)
    }
    fn execute(&mut self, _: Option<&str>, prompt: &str) -> Result<String, String> {
        self.turns += 1;
        self.prompts.push(prompt.into());
        Ok("session".into())
    }
    fn head(&mut self) -> Result<String, String> {
        let turn = self.turns.min(self.commit_turns);
        Ok(if turn == 0 {
            "base".into()
        } else {
            format!("head-{turn}")
        })
    }
    fn has_commits_beyond(&mut self, base: &str, head: &str) -> Result<bool, String> {
        Ok(base != head)
    }
    fn fast_checks(&mut self, head: &str) -> Result<(), String> {
        self.checked.push(head.into());
        if !self.checks.is_empty() {
            return Err(self.checks.remove(0).into());
        }
        self.stamped.push(head.into());
        Ok(())
    }
    fn stamped(&mut self, head: &str) -> bool {
        self.stamped.iter().any(|h| h == head)
    }
    fn review(&mut self, head: &str, _: Option<&Value>) -> Result<Value, String> {
        Ok(json!({"verdict":"ACCEPT","head":head,"run_id":"run-review","text":"VERDICT: ACCEPT\n"}))
    }
    fn gates(&mut self) -> Result<(), String> {
        Ok(())
    }
    fn infra_wait(&mut self, attempt: u32) {
        self.waits.push(attempt);
    }
    fn save(&mut self, _: &Value) -> Result<(), String> {
        Ok(())
    }
}

const SLOT: &str = "fast checks: all 2 cargo pool slots are busy; timed out after 30s";

#[test]
fn a_slot_timeout_reruns_the_gate_on_the_same_head_without_a_writer_turn() {
    let mut driver = fake(vec![SLOT]);
    let record = run(&mut driver, json!({}), "brief");
    assert_eq!(record["exit_code"], 0, "{record}");
    assert_eq!(driver.turns, 1, "no follow-up turn: {:?}", driver.prompts);
    assert_eq!(driver.checked, vec!["head-1", "head-1"]);
    assert_eq!(driver.waits, vec![1]);
    assert_eq!(record["fast_check_infra_reruns"], 1);
    assert_eq!(record["final_head"], "head-1");
}

#[test]
fn a_test_failure_still_sends_the_writer_a_follow_up() {
    let mut driver = fake(vec!["test result: FAILED. 1 passed; 1 failed"]);
    let record = run(&mut driver, json!({}), "brief");
    assert_eq!(record["exit_code"], 0, "{record}");
    assert_eq!(driver.turns, 2);
    assert!(driver.prompts[1].contains("The fast checks failed on your head head-1"));
    assert!(driver.waits.is_empty());
}

#[test]
fn a_slot_that_stays_busy_ends_gate_unavailable_never_no_commit_and_resume_checks_again() {
    let mut driver = Fake {
        commit_turns: 1,
        ..fake(vec![SLOT; INFRA_RERUNS as usize + 1])
    };
    let record = run(&mut driver, json!({}), "brief");
    let reason = record["reason"].as_str().unwrap();
    assert!(
        reason.starts_with("gate_unavailable: cargo pool slots are busy on head-1"),
        "{reason}"
    );
    assert_eq!(record["result"], "BLOCKED");
    assert_eq!(record["fast_checks"]["status"], "unavailable");
    assert_eq!(driver.turns, 1);
    // A resume whose turn leaves the head unchanged reruns the gate on it instead of
    // answering no_commit.
    let record = run_resume(&mut driver, record, "session", "resume");
    assert_eq!(record["exit_code"], 0, "{record}");
    assert_eq!(driver.turns, 2);
    assert_eq!(driver.stamped, vec!["head-1"]);
}

#[test]
fn infra_cause_names_only_infrastructure_refusals() {
    assert_eq!(infra_cause(SLOT), Some("cargo pool slots are busy"));
    assert_eq!(
        infra_cause("cargo lease unavailable: CargoLeaseTimeout"),
        Some("cargo lease unavailable")
    );
    assert_eq!(
        infra_cause(&format!("{SLOT}\nerror[E0308]: mismatched types")),
        None
    );
    assert_eq!(infra_cause("check_file_size.py: too large"), None);
}
