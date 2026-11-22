//! Render and validate the response from a review seat.
use super::SeatOutcome;
use pg_worker_core::review_split::{
    extract_review_text_from_live_log, parse_head_line_text, parse_test_gaming_text,
    parse_verdict_text, SeatVerdict, TestGaming,
};
use pg_worker_core::{review_completion, review_text::record_answer};
use serde_json::{json, Value};
use std::{
    fs,
    path::{Path, PathBuf},
};

fn extract_seat_text(record: &Value, run_dir: Option<&Path>) -> String {
    if let Some(dir) = run_dir {
        let review_txt = dir.join("seat").join("review.txt");
        if let Ok(content) = fs::read_to_string(review_txt) {
            if !content.trim().is_empty() {
                return content;
            }
        }
    }
    if let Some(answer) = record_answer(record) {
        return answer;
    }
    let live_log_path = record["live_log"]
        .as_str()
        .map(PathBuf::from)
        .or_else(|| run_dir.map(|d| d.join("live.log")));
    if let Some(path) = live_log_path {
        if let Ok(raw) = fs::read_to_string(path) {
            if let Some(parsed) = extract_review_text_from_live_log(&raw) {
                return parsed;
            }
            if !raw.trim().is_empty() {
                return raw;
            }
        }
    }
    String::new()
}

pub(super) fn evaluate_seat_record(
    index: usize,
    total: usize,
    expected_head: &str,
    paths: &[String],
    record: &Value,
    run_dir: Option<&Path>,
) -> SeatOutcome {
    let run_id = record["run_id"].as_str().unwrap_or("?").to_owned();
    let status = record["status"].as_str().unwrap_or("unknown").to_owned();
    let exit_code = record["exit_code"].as_i64().unwrap_or(-1);
    let leg_val = record
        .get("chosen_leg")
        .filter(|v| !v.is_null())
        .or_else(|| record.pointer("/plan/first_leg").filter(|v| !v.is_null()));
    let leg = leg_val
        .and_then(|v| v.get("id").and_then(Value::as_str).or_else(|| v.as_str()))
        .unwrap_or("none")
        .to_owned();
    let family = leg_val
        .and_then(|v| v.get("family").and_then(Value::as_str))
        .unwrap_or("unknown")
        .to_owned();

    let harness = leg_val
        .and_then(|leg| leg["harness"].as_str())
        .unwrap_or("unknown")
        .to_owned();
    let text = extract_seat_text(record, run_dir);
    let files_read = pg_worker_core::review_split::files_read_text(&text);
    let verdict = if review_completion::live(&status) {
        Some(SeatVerdict::Pending)
    } else {
        parse_verdict_text(&text).filter(|verdict| {
            *verdict != SeatVerdict::Pass || pg_worker_core::review_split::has_files_read(&text)
        })
    };
    let mut test_gaming = parse_test_gaming_text(&text);
    if test_gaming == TestGaming::Missing {
        if record["test_gaming"] == json!(false) || record["test_gaming"] == json!("no") {
            test_gaming = TestGaming::No;
        } else if record["test_gaming"] == json!(true) || record["test_gaming"] == json!("yes") {
            test_gaming = TestGaming::Yes;
        }
    }
    // The seat must echo the full head; a missing or redacted HEAD line is no evidence.
    let head_ok =
        parse_head_line_text(&text).is_some_and(|seen| seen.eq_ignore_ascii_case(expected_head));

    let passed = status == "completed"
        && exit_code == 0
        && verdict == Some(SeatVerdict::Pass)
        && test_gaming == TestGaming::No
        && head_ok;
    let paths_summary = if paths.is_empty() {
        "all".to_owned()
    } else if paths.len() <= 3 {
        paths.join(",")
    } else {
        format!("{}..+{}files", paths[0], paths.len() - 1)
    };

    SeatOutcome {
        index,
        total,
        run_id,
        status,
        exit_code,
        leg,
        family,
        harness,
        files_read,
        verdict,
        test_gaming,
        paths_summary,
        passed,
    }
}
