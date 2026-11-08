use pg_worker_core::run::record::{RunRecord, Status};
use serde_json::{json, Value};
use std::path::Path;

pub(crate) fn finish(record: &mut RunRecord, status: Status, leg: &Value, attempt_id: u32) {
    let diagnostic = crate::gitconfig_guard::guard_diagnostic(record, status, leg, attempt_id);
    let status = crate::gitconfig_guard::enforce(record, status, diagnostic);
    let mut attempts = match record.get("attempts") {
        None if attempt_id == 1 => vec![json!({"legacy_normalized":true})],
        None => Vec::new(),
        Some(Value::Array(attempts)) => attempts.clone(),
        Some(_) => {
            record.set("attempts_error", json!("attempts must be an array"));
            return;
        }
    };
    let Some(attempt) = attempt_id
        .checked_sub(1)
        .and_then(|index| attempts.get_mut(index as usize))
        .filter(|entry| entry.is_object())
    else {
        record.set("attempts_error", json!("unknown or invalid attempt id"));
        return;
    };
    let dry = status == Status::DryRun;
    let status = crate::gitconfig_guard::writer_completion(record, status);
    let unwitnessed = json!("unwitnessed");
    let completion = json!({"leg":leg,"status":status.as_str(),
        "served_model":if dry { &leg["model"] } else { &unwitnessed },
        "served_family":if dry { &leg["family"] } else { &unwitnessed },
        "served_witness_reason":"no authenticated harness telemetry; skipped or unobserved leg",
        "request_side_model":null,"witness_kind":null,
        "skip_reason":if status == Status::Completed || dry { None } else { record.get("reason") },
        "provider_response":null,"stdout_tail":record.get("stdout_tail"),"stderr_tail":record.get("stderr_tail")});
    attempt
        .as_object_mut()
        .unwrap()
        .extend(completion.as_object().unwrap().clone());
    record.set("attempts", json!(attempts));
    if record.get("attempts_error").is_some() {
        record.set("attempts_error", Value::Null);
    }
    let chosen = if status == Status::Completed || dry {
        leg.clone()
    } else {
        Value::Null
    };
    record.set("chosen_leg", chosen);
    record.set("writer_family", leg["family"].clone());
    if let Some(state) = record
        .path()
        .parent()
        .and_then(Path::parent)
        .and_then(Path::parent)
        .map(Path::to_owned)
    {
        let value = record.to_value();
        let (rows, skipped) = crate::evidence_row::adapted(&value);
        if !dry {
            record.set("evidence_unclassified_attempts", json!(skipped));
        }
        if value["role"] == "review" {
            let text = crate::review_artifacts::review_text(&state, &value)
                .unwrap_or_else(|_| "Full rendered review unavailable.\nVERDICT: HALT\n".into());
            let result = crate::review_artifacts::write_review_artifacts(&state, &value, &text);
            if result.is_ok() {
                record.set("seat_dir", json!(record.path().with_file_name("seat")));
            } else {
                record.set(
                    "review_artifact_error",
                    json!("review artifacts unavailable"),
                );
            }
        }
        let role = value["role"].as_str().unwrap_or("");
        if let Ok(store) =
            crate::evidence::EvidenceStore::open(&state, crate::evidence::enabled(&state, role))
        {
            if store.backfill(&crate::records::list_runs(&state)).is_err() {
                record.set("evidence_error", json!("evidence unavailable"));
            }
            for row in rows {
                if store.append(&row).is_err() {
                    record.set("evidence_error", json!("evidence unavailable"));
                }
            }
        }
    }
}
