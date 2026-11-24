//! Same-SHA retry classification: a failure that passes on the retry is a flake, one that fails again is red.
//! Neither turns the job green; the record and the Keeper item definition only describe what happened.
use crate::logs::{failed_ids, passed_ids};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

pub struct Run {
    pub suite: String,
    pub runner: String,
    pub run_id: String,
    pub attempt: String,
    pub sha: String,
    pub first_log: String,
    pub second_log: String,
}

/// `(test id, second result, flaky)` for every test the first run failed. A test the retry did not report
/// ("missing") counts as failed again.
pub fn classify(first: &str, second: &str) -> Vec<(String, &'static str, bool)> {
    let passed = passed_ids(second);
    let failed = failed_ids(second);
    failed_ids(first)
        .into_iter()
        .map(|id| {
            // An error or failure outranks a pass: pytest prints PASSED for the call and ERROR for a failed teardown.
            let result = if failed.contains(&id) {
                "failed"
            } else if passed.contains(&id) {
                "passed"
            } else {
                "missing"
            };
            let flaky = result == "passed";
            (id, result, flaky)
        })
        .collect()
}

/// `LE-FLAKE-<12 hex>`: one item per suite and test, so a second sighting finds the first.
pub fn item_key(suite: &str, id: &str) -> String {
    let digest = Sha256::digest(format!("{suite}:{id}"));
    let hex: String = digest.iter().take(6).map(|b| format!("{b:02X}")).collect();
    format!("LE-FLAKE-{hex}")
}

/// A priority-10 Keeper item definition (keeper define format) for a test that failed and then passed.
pub fn keeper_item(run: &Run, id: &str) -> Value {
    let title: String = format!(
        "Fix flaky {} test {id}: failed then passed on the same SHA",
        run.suite
    )
    .chars()
    .take(200)
    .collect();
    json!({
        "key": item_key(&run.suite, id),
        "project": "plangraph",
        "title": title,
        "kind": "task",
        "under": "plangraph:LESSONS-ENFORCE",
        "priority": 10,
        "description": format!(
            "Why: {id} failed and passed on a same-SHA retry (suite {}, runner {}, run {} attempt {}, SHA {}); the run stayed red. First log {}, retry log {}.\n\
             How: find the nondeterminism (shared state, ordering, timing, port, clock) with the two logs, fix it with a regression test for the whole class, and remove any quarantine entry once it holds.\n\
             Done when: {id} passes five same-head runs at part entry, including one full-worker run.",
            run.suite, run.runner, run.run_id, run.attempt, run.sha, run.first_log, run.second_log
        ),
    })
}

/// The record of one classification: per-test results, the verdict of the run and the item definitions.
pub fn record(run: &Run, first: &str, second: &str) -> Value {
    let tests = classify(first, second);
    let verdict = if tests.is_empty() {
        "fail-unclassified"
    } else if tests.iter().all(|t| t.2) {
        "fail-pass"
    } else {
        "fail-fail"
    };
    let items: Vec<Value> = tests
        .iter()
        .filter(|t| t.2)
        .map(|t| keeper_item(run, &t.0))
        .collect();
    json!({
        "suite": run.suite, "runner": run.runner, "run_id": run.run_id, "attempt": run.attempt, "sha": run.sha,
        "first_log": run.first_log, "second_log": run.second_log,
        "verdict": verdict, "result": "red",
        "tests": tests.iter().map(|(id, second, flaky)| json!({"id": id, "first": "failed", "second": second, "flaky": flaky})).collect::<Vec<_>>(),
        "keeper_items": items,
    })
}

/// A markdown summary for the job summary and the gate.
pub fn summary(record: &Value) -> String {
    let mut out = format!(
        "### Same-SHA retry: {} (the run stays red)\n\nRunner {}, run {} attempt {}, SHA {}. Logs: {} and {}.\n\n",
        record["verdict"].as_str().unwrap_or(""),
        record["runner"].as_str().unwrap_or(""),
        record["run_id"].as_str().unwrap_or(""),
        record["attempt"].as_str().unwrap_or(""),
        record["sha"].as_str().unwrap_or(""),
        record["first_log"].as_str().unwrap_or(""),
        record["second_log"].as_str().unwrap_or("")
    );
    for t in record["tests"].as_array().into_iter().flatten() {
        let tag = if t["flaky"].as_bool() == Some(true) {
            "FLAKY (failed, then passed)"
        } else {
            "FAILED again"
        };
        out += &format!(
            "- `{}`: {} (retry: {})\n",
            t["id"].as_str().unwrap_or(""),
            tag,
            t["second"].as_str().unwrap_or("")
        );
    }
    for item in record["keeper_items"].as_array().into_iter().flatten() {
        out += &format!(
            "\nKeeper item to define: `{}` priority 10\n",
            item["key"].as_str().unwrap_or("")
        );
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    fn run() -> Run {
        Run {
            suite: "pytest".into(),
            runner: "r-1".into(),
            run_id: "77".into(),
            attempt: "1".into(),
            sha: "a".repeat(40),
            first_log: "first.log".into(),
            second_log: "second.log".into(),
        }
    }

    #[test]
    fn fail_then_pass_is_flaky_stays_red_and_defines_a_priority_ten_item() {
        let record = record(&run(), "FAILED t.py::a - x\n", "PASSED t.py::a\n");
        assert_eq!(record["verdict"], "fail-pass");
        assert_eq!(record["result"], "red");
        let item = &record["keeper_items"][0];
        assert_eq!(item["priority"], 10);
        assert_eq!(item["under"], "plangraph:LESSONS-ENFORCE");
        assert!(item["key"].as_str().unwrap().starts_with("LE-FLAKE-"));
        let text = item["description"].as_str().unwrap();
        for needle in [
            "r-1",
            "run 77 attempt 1",
            &"a".repeat(40),
            "first.log",
            "second.log",
            "t.py::a",
        ] {
            assert!(text.contains(needle), "{needle}");
        }
    }

    #[test]
    fn fail_then_fail_stays_red_and_defines_no_item() {
        let record = record(&run(), "FAILED t.py::a - x\n", "FAILED t.py::a - x\n");
        assert_eq!(
            (record["verdict"].clone(), record["result"].clone()),
            ("fail-fail".into(), "red".into())
        );
        assert!(record["keeper_items"].as_array().unwrap().is_empty());
    }

    #[test]
    fn a_teardown_error_after_a_pass_is_a_failure_not_a_flake() {
        // pytest prints PASSED for the call and ERROR for the teardown of the same test id.
        let record = record(
            &run(),
            "FAILED t.py::a - x\n",
            "PASSED t.py::a\nERROR t.py::a - teardown\n",
        );
        assert_eq!(record["verdict"], "fail-fail");
        assert_eq!(record["tests"][0]["second"], "failed");
        assert!(record["keeper_items"].as_array().unwrap().is_empty());
    }

    #[test]
    fn a_test_the_retry_did_not_report_counts_as_failed_again() {
        let record = record(
            &run(),
            "FAILED t.py::a - x\nFAILED t.py::b - y\n",
            "PASSED t.py::a\n",
        );
        assert_eq!(record["verdict"], "fail-fail");
        assert_eq!(record["tests"][1]["second"], "missing");
        assert_eq!(record["keeper_items"].as_array().unwrap().len(), 1);
    }

    #[test]
    fn a_red_run_without_a_parsable_failure_is_unclassified_and_stays_red() {
        let record = record(&run(), "Killed\n", "");
        assert_eq!(
            (record["verdict"].clone(), record["result"].clone()),
            ("fail-unclassified".into(), "red".into())
        );
    }

    #[test]
    fn the_item_key_is_stable_per_suite_and_test_and_the_title_is_capped() {
        assert_eq!(item_key("pytest", "t.py::a"), item_key("pytest", "t.py::a"));
        assert_ne!(item_key("pytest", "t.py::a"), item_key("cargo", "t.py::a"));
        let long = "x".repeat(400);
        assert!(keeper_item(&run(), &long)["title"].as_str().unwrap().len() <= 200);
    }

    #[test]
    fn the_summary_names_flaky_tests_both_logs_and_the_item() {
        let text = summary(&record(&run(), "FAILED t.py::a - x\n", "PASSED t.py::a\n"));
        assert!(
            text.contains("FLAKY (failed, then passed)")
                && text.contains("first.log and second.log")
        );
        assert!(text.contains("LE-FLAKE-") && text.contains("the run stays red"));
    }
}
