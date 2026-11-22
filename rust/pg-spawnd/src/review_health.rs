//! Review-result diagnostics for routing and reporting. A verdict here is not an approval.
use pg_worker_core::{review_split::parse_verdict_text, review_text::record_answer};
use serde_json::{json, Value};
use std::{collections::BTreeMap, path::Path};

const WINDOW: usize = 10;

pub(crate) fn classify(record: &Value, text: &str) -> &'static str {
    let reason = record["reason"].as_str().unwrap_or("");
    let events = record["events"].as_array().into_iter().flatten();
    let last_step = events
        .clone()
        .rev()
        .find(|e| e["event"] == "opencode_step_finish");
    let last_frame = record["stdout_tail"]
        .as_str()
        .into_iter()
        .flat_map(str::lines)
        .rev()
        .filter_map(|line| serde_json::from_str::<Value>(line).ok())
        .find(|frame| frame["type"] == "step_finish");
    let finish = record["finish_reason"]
        .as_str()
        .or_else(|| {
            last_step.and_then(|step| step["finish_reason"].as_str().or(step["reason"].as_str()))
        })
        .or_else(|| {
            last_frame
                .as_ref()
                .and_then(|frame| frame["part"]["reason"].as_str())
        });
    if matches!(finish, Some("length" | "max_tokens"))
        || reason.starts_with("reasoning_output_limit")
        || events
            .clone()
            .any(|e| e["event"] == "opencode_output_limit")
    {
        "length_stop"
    } else if reason.contains("prompt_too_large") {
        "prompt_too_large"
    } else if reason.contains("missing diff_ref")
        || reason.contains("no diff_ref")
        || reason.contains("empty diff")
    {
        "missing_material"
    } else if record["status"] == "timeout"
        || record["exit_code"] == 124
        || matches!(reason, "timeout" | "max_time" | "max_s")
    {
        "timeout"
    } else if reason.contains("client_missing") {
        "client_missing"
    } else if record["review_status"] != "no_verdict"
        && record["exit_code"].as_i64().is_none_or(|code| code == 0)
        && parse_verdict_text(text).is_some()
    {
        "verdict"
    } else if events
        .clone()
        .any(|e| e["event"] == "opencode_tools_denied")
        && (finish == Some("tool-calls")
            || events.clone().any(|e| {
                e["event"] == "opencode_step_finish" && e["finish_reason"] == "tool-calls"
            }))
    {
        "tools_denied"
    } else if text.trim().is_empty() {
        "empty_reply"
    } else {
        "missing_verdict"
    }
}

type Sample<'a> = (f64, &'a str, &'a str);

fn samples<'a>(state: &Path, records: &'a [Value]) -> BTreeMap<&'a str, Vec<Sample<'a>>> {
    let mut legs: BTreeMap<_, Vec<_>> = BTreeMap::new();
    for record in records {
        if record["role"] != "review"
            || !matches!(
                record["status"].as_str(),
                Some("completed" | "failed" | "timeout" | "stalled" | "limited")
            )
        {
            continue;
        }
        let leg = record["attempts"]
            .as_array()
            .and_then(|attempts| attempts.last())
            .and_then(|attempt| attempt["leg"]["id"].as_str())
            .or_else(|| record["chosen_leg"]["id"].as_str());
        let (Some(leg), Some(id), Some(ended)) =
            (leg, record["run_id"].as_str(), record["ended"].as_f64())
        else {
            continue;
        };
        let class = record["review_result"]
            .as_str()
            .unwrap_or_else(|| classify(record, &answer(state, record)));
        legs.entry(leg).or_default().push((ended, id, class));
    }
    legs
}

pub(crate) fn answer(state: &Path, record: &Value) -> String {
    record_answer(record).unwrap_or_else(|| {
        crate::review_artifacts::review_text(state, record)
            .ok()
            .filter(|text| {
                text != crate::review_artifacts::DIAGNOSTIC_REVIEW_TEXT
                    && !text.starts_with("Agy review unavailable:")
            })
            .unwrap_or_default()
    })
}

pub(crate) fn report(state: &Path, records: &[Value], since: i64, until: i64) -> Value {
    let mut view = BTreeMap::new();
    for (leg, runs) in samples(state, records) {
        let in_range = runs
            .into_iter()
            .filter(|(ended, _, _)| *ended >= since as f64 && *ended < until as f64);
        let mut total = 0;
        let mut failures = Vec::new();
        for (_, run_id, cause) in in_range {
            total += 1;
            if cause != "verdict" {
                failures.push(json!({"run_id":run_id,"cause":cause}));
            }
        }
        if total > 0 {
            view.insert(
                leg,
                json!({"reviews":total,"no_verdict":failures.len(),"runs":failures}),
            );
        }
    }
    json!(view)
}

pub(crate) fn demote(state: &Path, plan: &mut Value, records: &[Value]) {
    let Some(legs) = plan["legs"].as_array_mut() else {
        return;
    };
    let mut history = samples(state, records);
    let mut alerts = BTreeMap::new();
    for leg in legs.iter().filter_map(|leg| leg["id"].as_str()) {
        let Some(runs) = history.get_mut(leg).filter(|runs| runs.len() >= WINDOW) else {
            continue;
        };
        runs.sort_unstable_by(|a, b| b.0.total_cmp(&a.0).then_with(|| a.1.cmp(b.1)));
        let failures: Vec<_> = runs[..WINDOW]
            .iter()
            .filter(|run| run.2 != "verdict")
            .map(|&(_, run_id, cause)| json!({"run_id":run_id, "cause":cause}))
            .collect();
        if failures.len() > 3 {
            alerts.insert(
                leg.to_owned(),
                json!({"leg":leg, "no_verdict":failures.len(),
                "reviews":WINDOW, "runs":failures}),
            );
        }
    }
    if !alerts.is_empty() {
        legs.sort_by_key(|leg| leg["id"].as_str().is_some_and(|id| alerts.contains_key(id)));
        if let Some(first) = legs.first().cloned() {
            plan["first_leg"] = first;
        }
    }
    plan["review_health_alerts"] = json!(alerts.into_values().collect::<Vec<_>>());
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn classifies_review_failures_from_record_evidence() {
        for (record, answer, expected) in [
            (
                json!({"events":[{"event":"opencode_step_finish","reason":"length"}]}),
                "Partial",
                "length_stop",
            ),
            (
                json!({"events":[{"event":"opencode_tools_denied"},{"event":"opencode_step_finish","finish_reason":"tool-calls"}]}),
                "Could not read the files",
                "tools_denied",
            ),
            (
                json!({"reason":"inline_diff_failed: missing diff_ref"}),
                "",
                "missing_material",
            ),
            (json!({"reason":"max_time"}), "", "timeout"),
            (json!({"reason":"prompt_too_large"}), "", "prompt_too_large"),
            (json!({}), "", "empty_reply"),
            (json!({}), "Findings without a decision", "missing_verdict"),
            (json!({}), "VERDICT: PASS", "verdict"),
        ] {
            assert_eq!(classify(&record, answer), expected);
        }
    }

    #[test]
    fn reports_run_ids_using_record_answers_before_log_fallback() {
        let state = tempfile::tempdir().unwrap();
        let run_dir = state.path().join("runs/run-passing");
        std::fs::create_dir_all(&run_dir).unwrap();
        let stream = run_dir.join("live.log");
        std::fs::write(
            &stream,
            concat!(
                "{\"type\":\"text-delta\",\"part\":{\"id\":\"p\",\"delta\":\"VER\"}}\n",
                "{\"type\":\"text-delta\",\"part\":{\"id\":\"p\",\"delta\":\"DICT: PASS\"}}\n",
                "{\"type\":\"step_finish\",\"part\":{\"reason\":\"stop\"}}\n"
            ),
        )
        .unwrap();
        let passing = json!({"role":"review", "status":"completed", "ended":3,
            "run_id":"run-passing", "chosen_leg":{"id":"mimo"}, "live_log":stream,
            "stdout_tail":"VERDICT: PASS"});
        let mut missing = passing.clone();
        missing["run_id"] = json!("run-missing");
        missing["stdout_tail"] = json!("Findings only");
        let report = report(state.path(), &[passing, missing], 0, 4);
        assert_eq!(
            report["mimo"],
            json!({"reviews":2, "no_verdict":1,
            "runs":[{"run_id":"run-missing", "cause":"missing_verdict"}]})
        );
    }

    #[test]
    fn demotes_only_after_four_of_ten_terminal_reviews_without_verdicts() {
        let mut plan = json!({"first_leg":{"id":"a"},"legs":[{"id":"a"},{"id":"b"}]});
        let mut records: Vec<_> = (0..11)
            .map(|i| {
                json!({
                    "role":"review", "status":"completed", "ended":i + 1,
                    "run_id":format!("run-{i}"), "chosen_leg":{"id":"a"},
                    "stdout_tail":if i < 5 { "" } else { "VERDICT: PASS" }
                })
            })
            .collect();
        demote(Path::new(""), &mut plan, &records);
        assert_eq!(plan["first_leg"]["id"], "b");
        assert_eq!(plan["review_health_alerts"][0]["no_verdict"], 4);
        assert_eq!(plan["review_health_alerts"][0]["leg"], "a");

        records[1]["status"] = json!("running");
        records[0]["stdout_tail"] = json!("VERDICT: PASS");
        let mut plan = json!({"first_leg":{"id":"a"},"legs":[{"id":"a"},{"id":"b"}]});
        demote(Path::new(""), &mut plan, &records);
        assert_eq!(plan["first_leg"]["id"], "a");
        assert_eq!(plan["review_health_alerts"], json!([]));
    }
}
