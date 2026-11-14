//! Operator ownership policy; the keeper supplies stored rows and trusted events.
use chrono::NaiveDate;
use serde_json::{json, Value};

const RISKS: &[&str] = &[
    "money",
    "access",
    "secret",
    "public",
    "irreversible",
    "live",
    "taste",
];
const HARD: &[&str] = &[
    "money",
    "access",
    "secret",
    "public",
    "irreversible",
    "live",
];
pub const GRANT: &str = "D-2579";

fn text<'a>(item: &'a Value, field: &str) -> &'a str {
    item[field].as_str().unwrap_or("")
}
fn flagged(item: &Value) -> bool {
    item["operator"] == true || item["operator"] == 1
}
fn operator(item: &Value) -> bool {
    text(item, "owner") == "operator" || flagged(item)
}
fn date(value: &Value) -> Option<NaiveDate> {
    let raw = value.as_str()?;
    if raw.len() != 10 {
        return None;
    }
    NaiveDate::parse_from_str(raw, "%Y-%m-%d").ok()
}
fn choices(item: &Value) -> Value {
    if let Some(raw) = item["choices"].as_str() {
        serde_json::from_str(raw).unwrap_or(Value::Null)
    } else {
        item["choices"].clone()
    }
}

/// Grant class identifiers can be carried in the existing track field.
/// Descriptive titles also catch the ordinary covered requests.
fn grant_class(item: &Value) -> Option<&'static str> {
    let track = text(item, "track");
    let title = text(item, "title").to_lowercase();
    let body = format!("{} {}", title, text(item, "description").to_lowercase());
    let words: Vec<_> = body
        .split(|c: char| !c.is_alphanumeric())
        .filter(|s| !s.is_empty())
        .collect();
    let has = |word: &str| words.contains(&word);
    if track == "merge_with_go" || ((has("merge") || has("merges")) && has("go")) {
        Some("merge_with_go")
    } else if track == "service_restart"
        || ((has("restart") || has("restarts"))
            && (has("service") || has("unit") || has("systemd")))
    {
        Some("service_restart")
    } else if track == "secret_creation_until_phase1"
        || ((has("create") || has("creation"))
            && (has("secret") || has("secrets"))
            && (body.contains("until phase 1") || body.contains("until phase1")))
    {
        Some("secret_creation_until_phase1")
    } else if track == "continuity_go_within_budget"
        || (has("continuity") && has("go") && body.contains("within budget"))
    {
        Some("continuity_go_within_budget")
    } else {
        None
    }
}

fn trusted_answer(request: &Value, before: &Value) -> bool {
    let Some(events) = request["events"].as_array() else {
        return false;
    };
    let principal = text(request, "operator_principal");
    if principal.is_empty() {
        return false;
    }
    let mut latest_change = 0;
    let mut answer = 0;
    for event in events {
        let payload = if let Some(raw) = event["payload"].as_str() {
            serde_json::from_str(raw).unwrap_or(Value::Null)
        } else {
            event["payload"].clone()
        };
        let seq = event["seq"].as_u64().unwrap_or(0);
        if text(event, "type") == "item_defined"
            && payload["fields"].as_array().is_some_and(|fields| {
                fields.iter().any(|f| {
                    ["owner", "operator", "risk_tag"]
                        .iter()
                        .any(|name| f == name)
                })
            })
        {
            latest_change = latest_change.max(seq);
        }
        if text(event, "type") == "question_answered"
            && text(event, "actor") == principal
            && payload["source"] == "answer"
            && text(event, "role") == "router"
            && payload["risk_tag"] == before["risk_tag"]
            && payload["epoch"] == before["epoch"]
        {
            answer = answer.max(seq);
        }
    }
    answer > latest_change
}

/// `mode` defaults to warn; refuse is an explicit rollout switch.
pub fn handle(request: Value) -> Result<Value, &'static str> {
    if !request.is_object() {
        return Err("invalid request");
    }
    let mode = request["mode"].as_str().unwrap_or("warn");
    if !["warn", "refuse"].contains(&mode) {
        return Err("invalid mode");
    }
    if let Some(items) = request["items"].as_array() {
        let today = date(&request["today"]).ok_or("invalid today")?;
        let hidden: Vec<_> = items
            .iter()
            .filter(|item| date(&item["until"]).is_some_and(|until| until > today))
            .map(|item| item["key"].clone())
            .collect();
        return Ok(json!({"hidden": hidden}));
    }
    let after = &request["after"];
    if !after.is_object() {
        return Err("invalid item");
    }
    let before = &request["before"];
    let mut findings = Vec::new();
    if operator(before)
        && HARD.contains(&text(before, "risk_tag"))
        && ((text(before, "owner") == "operator" && text(after, "owner") != "operator")
            || (flagged(before) && !flagged(after))
            || before["risk_tag"] != after["risk_tag"])
        && !trusted_answer(&request, before)
    {
        findings.push(
            "hard risk needs a trusted operator answer before ownership or risk changes"
                .to_string(),
        );
    }
    if operator(after) {
        if !["question", "operator_task"].contains(&text(after, "kind"))
            || text(after, "id").starts_with("RULE-")
        {
            findings.push(
                "operator ownership requires question/operator_task; rule and RULE-* are forbidden"
                    .to_string(),
            );
        }
        if !RISKS.contains(&text(after, "risk_tag")) {
            findings.push(format!("risk_tag must be one of {}", RISKS.join(",")));
        }
        let choice = choices(after);
        if choice["default"]
            .as_str()
            .is_none_or(|s| s.trim().is_empty())
            || date(&choice["deadline"]).is_none()
        {
            findings.push("choices.default and choices.deadline are required".to_string());
        }
        if let Some(class) = grant_class(after) {
            findings.push(format!("standing grant {GRANT} covers {class}"));
        }
    }
    if !after["until"].is_null() && date(&after["until"]).is_none() {
        findings.push("until must be YYYY-MM-DD or null".to_string());
    }
    let warnings: Vec<_> = findings
        .iter()
        .map(|s| format!("operator_owner_gate: {s}"))
        .collect();
    Ok(json!({"refused": mode == "refuse" && !warnings.is_empty(), "warnings": warnings}))
}
