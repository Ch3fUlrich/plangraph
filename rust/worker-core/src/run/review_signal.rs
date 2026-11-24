//! Review lifecycle fields shared by local health replay and the fleet journal.
use crate::{
    review_split::{parse_verdict_text, SeatVerdict},
    review_text::record_answer,
};
use serde_json::{json, Value};

pub fn completed(status: &Value) -> bool {
    matches!(
        status.as_str(),
        Some("completed" | "succeeded" | "terminated_after_answer")
    )
}

pub fn serving_leg(record: &Value) -> Option<&str> {
    let attempts = record["attempts"].as_array().into_iter().flatten();
    if let Some(attempt) = attempts
        .clone()
        .rev()
        .find(|a| a["status"].is_string() && a["status"] != "skipped")
    {
        if !completed(&attempt["status"]) {
            return None;
        }
        attempt["leg"]["id"]
            .as_str()
            .or_else(|| attempt["leg"].as_str())
    } else if attempts.clone().any(|a| a["status"] == "skipped") {
        None
    } else {
        record["chosen_leg"]["id"]
            .as_str()
            .or_else(|| record["chosen_leg"].as_str())
    }
}

pub fn answer_verdict(record: &Value, answer: Option<&str>) -> Option<&'static str> {
    if !completed(&record["status"])
        || record["review_status"] == "no_verdict"
        || record["review_result"]
            .as_str()
            .is_some_and(|r| r != "verdict")
    {
        return None;
    }
    let parsed = answer.and_then(parse_verdict_text);
    match parsed {
        Some(SeatVerdict::Pass) => Some("pass"),
        Some(SeatVerdict::RequestChanges | SeatVerdict::Fail) => Some("changes"),
        Some(SeatVerdict::Pending) => None,
        None => match record["verdict"].as_str() {
            Some("pass" | "ACCEPT") => Some("pass"),
            Some("fail" | "reject" | "REQUEST_CHANGES") => Some("changes"),
            _ => None,
        },
    }
}

/// Export only review facts. The answer, requested credentials and checkout paths
/// stay on the serving host.
pub fn fields(record: &Value) -> Value {
    let mut out = json!({});
    if record["origin_receipt"] == true || record["overflow_remote"] == true {
        out["origin_receipt"] = json!(true);
        return out;
    }
    if record["role"] == "review" {
        out["review_activity"] = json!(active(record));
        if let Some(leg) = serving_leg(record).filter(|_| completed(&record["status"])) {
            let verdict = record["review_signal_verdict"]
                .as_str()
                .filter(|v| matches!(*v, "pass" | "changes"))
                .or_else(|| answer_verdict(record, record_answer(record).as_deref()));
            if let Ok(binding) = serde_json::from_value::<crate::counted_seat::Binding>(
                record["counted_review"].clone(),
            ) {
                if record["run_id"] == binding.run_id
                    && record["head_sha"] == binding.head
                    && binding.selection.seat.leg == leg
                {
                    out["counted_review"] = json!(binding);
                }
            }
            out["review_terminal"] = json!({
                "leg":leg, "head":record["head_sha"], "verdict":verdict,
                "keeper_bound": !record["request"]["verdict_binding"].is_null()
            });
        }
    }
    if record["run_kind"] == "native-codex" && record["role"] == "implement" {
        if let Some(reviews) = record["reviews"].as_array() {
            out["native_review_rounds"] = json!(reviews
                .iter()
                .enumerate()
                .filter(|(_, r)| r["run_id"].is_string() && r["head"].is_string())
                .map(|(index, r)| json!({
                    "run_id":r["run_id"], "head":r["head"], "leg":r["leg"],
                    "verdict":r["verdict"], "round":index+1, "accepted_at":r["accepted_at"]
                }))
                .collect::<Vec<_>>());
        }
    }
    out
}

fn active(record: &Value) -> bool {
    if !record["started"].is_number()
        || matches!(
            record["status"].as_str(),
            Some(
                "queued"
                    | "queue_claimed"
                    | "dry_run"
                    | "refused"
                    | "skipped"
                    | "stalled_no_leg"
                    | "planned"
            )
        )
    {
        return false;
    }
    completed(&record["status"])
        || record["pid"].is_number()
        || record["attempts"].as_array().is_some_and(|attempts| {
            attempts.iter().any(|a| {
                matches!(
                    a["status"].as_str(),
                    Some("running" | "completed" | "succeeded" | "terminated_after_answer")
                ) || a["pid"].is_number()
            })
        })
}
