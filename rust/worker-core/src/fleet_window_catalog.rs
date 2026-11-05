//! Read-only v1 contract at `root/fleet/window-catalog.v1.json`; no location fallback.
//! `decode` parses JSON, `validate` records evidence, `capture` reads and returns JSON bytes.
//! Input: {schema_version:1, observed_at:UnixSeconds, entries:[{identity:{role,model,harness},
//! base:{window:{Known:positive_integer}|{Unknown:reason}, unit:"tokens", observed_at,
//! expires_at, provenance}, override?:same, served:[{model,protocol,family,proving_run:
//! {id,observed_at,provenance}}]}]}. All strings must be nonempty. Times are integral.
//! Output retains input evidence in `evidence`, plus selected windows/source, coverage and
//! structured alarm findings. Missing evidence is Unknown, never an inferred alias/family.
//! Revalidate the serialized capture to retain its separate clock watermark.
//! Invalid supplied overrides poison selection, not the retained base evidence.
//! S6/S12 consume this record, never invented windows. Later Rust consumers use this API;
//! private Python scripts cannot import Rust with SourceFileLoader: read JSON or use a
//! later thin binary. No binary, threshold policy, enable flag or session action lives here.
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::path::Path;
pub const RELATIVE_PATH: &str = "fleet/window-catalog.v1.json";
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub enum Window {
    Known(u64),
    Unknown(String),
}
#[derive(Debug, Serialize, Deserialize)]
pub struct Finding {
    pub identity: Value,
    pub source: String,
    pub reason: String,
}
#[derive(Debug, Serialize, Deserialize)]
pub struct Record {
    pub identity: Value,
    pub window: Window,
    pub selected_source: String,
    pub coverage: Vec<Coverage>,
}
#[derive(Debug, Serialize, Deserialize)]
pub struct Coverage {
    pub model: Value,
    pub protocol: Value,
    pub family: Value,
    pub proving_run: Value,
    pub window: Window,
}
#[derive(Debug, Serialize, Deserialize)]
pub struct Capture {
    pub schema_version: u8,
    pub clock_watermark: i64,
    pub evidence: Value,
    pub records: Vec<Record>,
    pub findings: Vec<Finding>,
}
fn text(v: &Value) -> bool {
    v.as_str().is_some_and(|s| !s.trim().is_empty())
}
fn time(v: &Value, now: i64) -> Result<(), &'static str> {
    let observed = v["observed_at"].as_i64().ok_or("invalid_observation")?;
    let expires = v["expires_at"].as_i64().ok_or("invalid_expiry")?;
    if expires <= observed {
        return Err("invalid_interval");
    }
    if observed > now {
        return Err("future_observation");
    }
    if now >= expires {
        return Err("expired");
    }
    if !text(&v["provenance"]) {
        return Err("missing_provenance");
    }
    Ok(())
}
fn window(v: &Value, now: i64) -> Result<Window, &'static str> {
    if v.is_null() {
        return Err("missing_field");
    }
    if v["unit"] != "tokens" {
        return Err("invalid_unit");
    }
    time(v, now)?;
    match serde_json::from_value::<Window>(v["window"].clone()) {
        Ok(Window::Known(n)) if n > 0 => Ok(Window::Known(n)),
        Ok(Window::Unknown(s)) if !s.trim().is_empty() => Ok(Window::Unknown(s)),
        _ => Err("invalid_window"),
    }
}
fn alarm(out: &mut Capture, identity: &Value, source: &str, reason: &str) -> Window {
    out.findings.push(Finding {
        identity: identity.clone(),
        source: source.into(),
        reason: reason.into(),
    });
    Window::Unknown(reason.into())
}
/// Validate independently; global schema/clock faults make every selection unknown.
pub fn validate(evidence: Value, now_s: i64) -> Capture {
    let watermark = evidence["clock_watermark"].as_i64().unwrap_or(now_s);
    let evidence = evidence.get("evidence").cloned().unwrap_or(evidence);
    let mut out = Capture {
        schema_version: 1,
        clock_watermark: now_s.max(watermark),
        evidence: evidence.clone(),
        records: vec![],
        findings: vec![],
    };
    let global = if evidence["schema_version"] != json!(1) {
        Some("invalid_schema")
    } else if evidence["observed_at"].as_i64().is_none() {
        Some("invalid_observation")
    } else if evidence["observed_at"].as_i64().unwrap() > now_s || watermark > now_s {
        Some("clock_backwards")
    } else {
        None
    };
    let Some(entries) = evidence["entries"].as_array().filter(|e| !e.is_empty()) else {
        let w = alarm(
            &mut out,
            &Value::Null,
            RELATIVE_PATH,
            global.unwrap_or("missing_entries"),
        );
        out.records.push(Record {
            identity: Value::Null,
            window: w,
            selected_source: "unknown".into(),
            coverage: vec![],
        });
        return out;
    };
    for (i, entry) in entries.iter().enumerate() {
        let id = &entry["identity"];
        let source = format!("{RELATIVE_PATH}#/entries/{i}");
        let duplicate = entries
            .iter()
            .enumerate()
            .any(|(j, other)| j != i && other["identity"] == *id && other != entry);
        if entries
            .iter()
            .enumerate()
            .any(|(j, other)| j != i && other == entry)
        {
            alarm(&mut out, id, &source, "duplicate_identical");
        }
        let invalid_id = ["role", "model", "harness"].iter().any(|k| !text(&id[k]));
        let selected = if entry.get("override").is_some() {
            "override"
        } else {
            "base"
        };
        let result = if let Some(reason) = global {
            Err(reason)
        } else if invalid_id {
            Err("missing_identity")
        } else if duplicate {
            Err("duplicate_conflict")
        } else {
            window(&entry[selected], now_s)
        };
        let w = match result {
            Ok(w) => w,
            Err(reason) => alarm(&mut out, id, &format!("{source}/{selected}"), reason),
        };
        let mut coverage = vec![];
        if let Some(served) = entry["served"].as_array().filter(|s| !s.is_empty()) {
            for (j, s) in served.iter().enumerate() {
                let proof = &s["proving_run"];
                let proved = global.is_none()
                    && !invalid_id
                    && !duplicate
                    && s["model"] == id["model"]
                    && ["model", "protocol", "family"].iter().all(|k| text(&s[k]))
                    && text(&proof["id"])
                    && text(&proof["provenance"])
                    && proof["observed_at"].as_i64().is_some_and(|t| t <= now_s)
                    && time(&entry[selected], now_s).is_ok();
                let window = if proved {
                    w.clone()
                } else {
                    alarm(
                        &mut out,
                        id,
                        &format!("{source}/served/{j}"),
                        "unproved_coverage",
                    )
                };
                coverage.push(Coverage {
                    model: s["model"].clone(),
                    protocol: s["protocol"].clone(),
                    family: s["family"].clone(),
                    proving_run: proof.clone(),
                    window,
                });
            }
        } else {
            let window = alarm(
                &mut out,
                id,
                &format!("{source}/served"),
                "missing_coverage",
            );
            coverage.push(Coverage {
                model: Value::Null,
                protocol: Value::Null,
                family: Value::Null,
                proving_run: Value::Null,
                window,
            });
        }
        out.records.push(Record {
            identity: id.clone(),
            window: w,
            selected_source: selected.into(),
            coverage,
        });
    }
    out
}
/// Malformed bytes are captured as an explicit source-level unknown and alarm.
pub fn decode(bytes: &[u8], now_s: i64) -> Capture {
    match serde_json::from_slice(bytes) {
        Ok(v) => validate(v, now_s),
        Err(_) => failure("invalid_json", now_s),
    }
}
fn failure(reason: &str, now_s: i64) -> Capture {
    let mut out = validate(Value::Null, now_s);
    out.findings.clear();
    out.records[0].window = alarm(&mut out, &Value::Null, RELATIVE_PATH, reason);
    out
}
/// Only reads the injected root. No source/destination writes or service queries.
pub fn capture(root: &Path, now_s: i64) -> Result<Vec<u8>, serde_json::Error> {
    let out = match std::fs::read(root.join(RELATIVE_PATH)) {
        Ok(bytes) => decode(&bytes, now_s),
        Err(_) => failure("unreadable_source", now_s),
    };
    serde_json::to_vec(&out)
}
#[cfg(test)]
#[path = "fleet_window_catalog_tests.rs"]
mod tests;
