//! Writer turns that leave HEAD unchanged: resumed on their items, never gated or reviewed again.
use super::*;

#[test]
fn a_turn_without_a_commit_resumes_on_the_items_and_runs_no_gate() {
    let mut driver = Fake {
        uncommitted_turns: 1,
        ..fake()
    };
    let record = run(&mut driver, json!({}), "brief");
    assert_eq!(record["exit_code"], 0);
    assert_eq!(driver.calls, vec![None, Some("same-session".into())]);
    assert!(driver.prompts[1].contains("No commit yet"));
    assert!(driver.prompts[1].contains("items of your brief"));
    // No fast check ran on the base head, and the only review was on the committed head.
    assert!(driver.stamped.iter().all(|head| head != "base"));
    assert_eq!(driver.reviews.len(), 1);
}
#[test]
fn a_fix_turn_without_a_commit_is_resumed_not_reviewed_again() {
    // The first review requests changes; the fix turn commits nothing (HEAD unchanged).
    struct Idle(Fake);
    impl Driver for Idle {
        fn prepare(&mut self) -> Result<String, String> {
            self.0.prepare()
        }
        fn refresh_base(&mut self) -> Result<String, String> {
            self.0.refresh_base()
        }
        fn disk_gb(&mut self) -> Result<u64, String> {
            self.0.disk_gb()
        }
        fn execute(&mut self, session: Option<&str>, prompt: &str) -> Result<String, String> {
            self.0.execute(session, prompt)
        }
        fn head(&mut self) -> Result<String, String> {
            // The first turn commits; the fix turns commit nothing until the second resume.
            Ok(match self.0.calls.len() {
                0 => "base",
                1..=3 => "final-head",
                _ => "fixed-head",
            }
            .into())
        }
        fn fast_checks(&mut self, head: &str) -> Result<(), String> {
            self.0.fast_checks(head)
        }
        fn stamped(&mut self, head: &str) -> bool {
            self.0.stamped(head)
        }
        fn review(
            &mut self,
            head: &str,
            previous: Option<&serde_json::Value>,
        ) -> Result<serde_json::Value, String> {
            self.0.review(head, previous)
        }
        fn gates(&mut self) -> Result<(), String> {
            self.0.gates()
        }
        fn save(&mut self, record: &serde_json::Value) -> Result<(), String> {
            self.0.save(record)
        }
    }
    let mut driver = Idle(Fake {
        verdicts: vec![("REQUEST_CHANGES", 2)],
        ..fake()
    });
    let record = run(&mut driver, json!({}), "brief");
    assert_eq!(record["exit_code"], 0, "{record}");
    // writer, review fix (no commit), two resumes on the items (the second commits).
    assert_eq!(driver.0.calls.len(), 4);
    assert!(driver.0.prompts[2].contains("No commit yet"));
    // Two reviews only: the unchanged head was never reviewed twice.
    assert_eq!(driver.0.reviews.len(), 2);
}
#[test]
fn a_relaunch_turn_without_a_commit_compares_against_the_head_before_it() {
    // Attempt 1 commits h1 and its fix turn crashes; the relaunch keeps h1 and its first turn
    // changes nothing: it is resumed on its items, h1 is not reviewed a second time.
    let mut driver = Fake {
        verdicts: vec![("REQUEST_CHANGES", 2)],
        head_script: vec!["base", "h1", "h1", "h1", "h2"],
        ..fake()
    };
    let record = {
        let mut wrapped = CrashOnSecond { inner: &mut driver };
        run(&mut wrapped, json!({}), "brief")
    };
    assert_eq!(record["exit_code"], 0, "{record}");
    assert_eq!(driver.calls.len(), 4);
    assert!(driver.prompts[2].contains("Open findings of the last review"));
    assert!(driver.prompts[3].contains("No commit yet"));
    assert_eq!(driver.reviews.len(), 2);
    assert_eq!(record["final_head"], "h2");
}
#[test]
fn a_writer_that_never_commits_fails_without_running_a_gate() {
    let mut driver = Fake {
        uncommitted_turns: 9,
        ..fake()
    };
    let record = run(&mut driver, json!({}), "brief");
    assert_eq!(record["exit_code"], 7);
    assert!(record["reason"].as_str().unwrap().starts_with("no_commit"));
    assert_eq!(driver.calls.len(), 1 + NO_COMMIT_RESUMES);
    assert!(driver.stamped.is_empty() && driver.reviews.is_empty());
}
