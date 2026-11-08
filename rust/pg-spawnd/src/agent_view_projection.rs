//! Public scalar-only projection for the independent agent inventory.
use regex::RegexBuilder;
use serde_json::{json, Value};
use std::path::Path;

// Never expose private patterns or source strings in diagnostics.
pub(super) fn mask_words(data: &mut Value, path: &Path) {
    let patterns = std::fs::read_to_string(path).ok().and_then(|text| {
        text.lines()
            .filter_map(|line| {
                let word = line.trim();
                if word.is_empty() || word.starts_with('#') {
                    return None;
                }
                let (pattern, token) = match word.strip_prefix("re:") {
                    Some(pattern) => (pattern.to_owned(), false),
                    None => (regex::escape(word), true),
                };
                Some(
                    RegexBuilder::new(&pattern)
                        .case_insensitive(true)
                        .build()
                        .map(|regex| (regex, token)),
                )
            })
            .collect::<Result<Vec<_>, _>>()
            .ok()
    });
    let mask = |text: &str| {
        let mut spans = Vec::new();
        for (regex, token) in patterns.as_ref().into_iter().flatten() {
            for hit in regex.find_iter(text) {
                if *token
                    && (text[..hit.start()]
                        .chars()
                        .next_back()
                        .is_some_and(char::is_alphanumeric)
                        || text[hit.end()..]
                            .chars()
                            .next()
                            .is_some_and(char::is_alphanumeric))
                {
                    continue;
                }
                spans.push((hit.start(), hit.end()));
            }
        }
        spans.sort_unstable();
        let mut merged: Vec<(usize, usize)> = Vec::new();
        for (start, end) in spans {
            if let Some(last) = merged.last_mut().filter(|last| start < last.1) {
                last.1 = last.1.max(end);
            } else {
                merged.push((start, end));
            }
        }
        let mut result = text.to_owned();
        for (start, end) in merged.into_iter().rev() {
            result.replace_range(start..end, "[redacted]");
        }
        result
    };
    for host in data["hosts"].as_array_mut().into_iter().flatten() {
        if patterns.is_none() {
            *host = unavailable("[redacted]", "scrub_words_unavailable");
            continue;
        }
        if let Some(name) = host["host"].as_str() {
            host["host"] = json!(mask(name));
        }
        for row in host["agents"].as_array_mut().into_iter().flatten() {
            for value in row
                .as_object_mut()
                .into_iter()
                .flat_map(|row| row.values_mut())
            {
                if let Some(text) = value.as_str() {
                    *value = json!(mask(text));
                }
            }
        }
    }
}
pub(super) fn unavailable(host: &str, reason: &str) -> Value {
    json!({"host":host,"readable":false,"reason":reason,"agents":[]})
}
#[rustfmt::skip]
pub(super) fn project( source: &Value, host: &str, runtime: Option<&str>, now: Option<f64>,
) -> Value { let schema: Value = serde_json::from_str(include_str!("../../../src/plangraph/web/agents_schema.json")).expect("schema"); let mut row = json!({}); for key in schema["fields"].as_array().unwrap().iter().map(|v| v.as_str().unwrap()) { let value = &source[key]; row[key] = if value.is_string() || value.is_number() {     value.clone() } else {     Value::Null }; } row["host"] = json!(host); row["runtime"] = json!(runtime.or_else(|| source["runtime"].as_str().filter(|s| schema["runtimes"].as_array().unwrap().iter().any(|v| v.as_str() == Some(*s))))); if let Some(now) = now { for (key, alias) in [("name", "run_id"), ("item", "item_key")] {     if row[key].is_null() && source[alias].is_string() {         row[key] = source[alias].clone();     } } row["state"] = json!(source.get("state").unwrap_or(&source["status"]).as_str()); let stamp = source     .get("started")     .or_else(|| source.get("launched_ts"))     .unwrap_or(&source["createdAt"]); let stamp = stamp.as_f64().or_else(|| {     stamp.as_str().and_then(|s| {         s.parse::<f64>()             .ok()             .or_else(|| crate::health_cli::timestamp(s).ok().map(|n| n as f64))     }) }); row["age_s"] = json!(stamp.filter(|s| s.is_finite() && *s <= now).map(|s| now - s)); } row
}
pub(super) fn project_view(data: &Value) -> Value {
    let hosts: Vec<_> = data["hosts"]
        .as_array()
        .into_iter()
        .flatten()
        .map(|entry| {
            let host = entry["host"].as_str().unwrap_or("unknown");
            match entry["agents"]
                .as_array()
                .filter(|_| entry["readable"] == true)
            {
                Some(rows) => json!({"host":host,"readable":true,"reason":null,
                "agents":rows.iter().map(|r| project(r,host,None,None)).collect::<Vec<_>>()}),
                None => unavailable(host, "unreadable"),
            }
        })
        .collect();
    json!({"generated_at":data["generated_at"].as_str(),"hosts":hosts})
}
#[cfg(test)]
#[path = "agent_view_projection_tests.rs"]
mod tests;
