//! Render review output from the stored answer or the harness event stream.
use serde_json::Value;

/// Return material already saved by the runner. Diagnostic-only frames are not answer text.
pub fn record_answer(record: &Value) -> Option<String> {
    let raw = record["stdout_tail"]
        .as_str()
        .filter(|s| !s.trim().is_empty())?;
    if raw.trim_start().starts_with('{') {
        extract_review_text_from_live_log(raw)
    } else {
        Some(raw.to_owned())
    }
}

/// Extract rendered review answer text from claude, agy, or opencode `live.log` NDJSON.
pub fn extract_review_text_from_live_log(live_log: &str) -> Option<String> {
    let mut result: Option<String> = None;
    let mut oc_event_log = false;
    let mut oc_texts: Vec<String> = Vec::new();
    let mut delta_id: Option<String> = None;
    let mut previous_was_anonymous = false;
    let mut oc_completed: Option<String> = None;
    let mut oc_stopped = false;

    for line in live_log.lines() {
        if line.trim().is_empty() {
            continue;
        }
        let Ok(frame) = serde_json::from_str::<Value>(line) else {
            return None;
        };
        let prior_was_anonymous = std::mem::take(&mut previous_was_anonymous);
        if frame["event"] == "result" {
            result = if frame["result"]["status"] == "SUCCESS" {
                frame["result"]["response"].as_str().map(str::to_owned)
            } else {
                None
            };
        } else if frame["type"] == "result" {
            result = if frame["is_error"] == false {
                frame["result"].as_str().map(str::to_owned)
            } else {
                None
            };
        } else if let (Some(kind), Some(part)) = (
            frame["type"].as_str(),
            frame.get("part").and_then(Value::as_object),
        ) {
            match kind {
                "step_start" => {
                    oc_event_log = true;
                    oc_texts.clear();
                    delta_id = None;
                    if !oc_stopped {
                        oc_completed = None;
                    }
                }
                "text" | "text-delta" | "text_delta" => {
                    if let Some(text) = part.get("text").and_then(Value::as_str) {
                        oc_texts.push(text.to_owned());
                        delta_id = None;
                        if !text.trim().is_empty() {
                            oc_completed = None;
                        }
                    } else if let Some(delta) = part.get("delta").and_then(Value::as_str) {
                        let id = part.get("id").and_then(Value::as_str);
                        let continues_part =
                            id.map_or(prior_was_anonymous, |id| delta_id.as_deref() == Some(id));
                        match oc_texts.last_mut() {
                            Some(text) if continues_part => text.push_str(delta),
                            _ => oc_texts.push(delta.to_owned()),
                        }
                        delta_id = id.map(str::to_owned);
                        previous_was_anonymous = id.is_none();
                        oc_completed = None;
                    }
                }
                "tool_use" => {
                    oc_completed = None;
                }
                "step_finish" => {
                    oc_event_log = true;
                    oc_stopped = part.get("reason").and_then(Value::as_str) == Some("stop");
                    if oc_stopped {
                        if oc_texts.iter().any(|t| !t.trim().is_empty()) {
                            oc_completed = Some(oc_texts.join("\n"));
                        }
                    } else {
                        oc_completed = Some(oc_texts.join("\n"));
                    }
                }
                _ => {}
            }
        }
    }

    if let Some(text) = result {
        return Some(text);
    }
    if oc_event_log {
        return oc_completed.filter(|t| !t.trim().is_empty());
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn split_log(id: Option<&str>) -> String {
        let delta = |text| match id {
            Some(id) => json!({"type":"text","part":{"id":id,"delta":text}}),
            None => json!({"type":"text","part":{"delta":text}}),
        };
        [
            json!({"type":"step_start","part":{}}),
            delta("VER"),
            delta("DICT: PASS"),
            json!({"type":"step_finish","part":{"reason":"stop"}}),
        ]
        .iter()
        .map(ToString::to_string)
        .collect::<Vec<_>>()
        .join("\n")
    }

    #[test]
    fn rendered_record_takes_precedence_over_split_stream() {
        let log = split_log(Some("text-1"));
        assert!(!log.contains("VERDICT:"));
        let available = json!({"stdout_tail":"VERDICT: PASS"});
        assert_eq!(record_answer(&available).as_deref(), Some("VERDICT: PASS"));
        let missing = json!({"stdout_tail":"Findings only"});
        assert_eq!(record_answer(&missing).as_deref(), Some("Findings only"));
        assert_eq!(record_answer(&json!({"stdout_tail":"  "})), None);
    }

    #[test]
    fn joins_deltas_of_the_same_text_part() {
        assert_eq!(
            extract_review_text_from_live_log(&split_log(Some("text-1"))).as_deref(),
            Some("VERDICT: PASS")
        );
    }
    #[test]
    fn joins_consecutive_deltas_without_part_ids() {
        let log = split_log(None);
        assert_eq!(
            extract_review_text_from_live_log(&log).as_deref(),
            Some("VERDICT: PASS")
        );
    }
    #[test]
    fn identified_part_continues_across_metadata() {
        let log = split_log(Some("text-1"));
        let mut lines: Vec<_> = log.lines().collect();
        lines.insert(2, r#"{"type":"heartbeat"}"#);
        assert_eq!(
            extract_review_text_from_live_log(&lines.join("\n")).as_deref(),
            Some("VERDICT: PASS")
        );
    }
}
