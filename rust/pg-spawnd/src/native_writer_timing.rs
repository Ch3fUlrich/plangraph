use super::Driver;
use serde_json::{json, Value};

/// Persist the interval even when a writer exits with an error. An open
/// interval identifies the current turn while Codex is executing.
pub(super) fn execute_turn(
    driver: &mut impl Driver,
    record: &mut Value,
    session: Option<&str>,
    prompt: &str,
) -> Result<String, (i32, String)> {
    let values = record.as_object_mut().unwrap();
    let turns = values.entry("writer_turns").or_insert_with(|| json!([]));
    let turns = turns
        .as_array_mut()
        .ok_or((6, "invalid writer turn history".into()))?;
    let index = turns.len();
    turns.push(json!({"started": chrono::Utc::now().to_rfc3339()}));
    driver.save(record).map_err(|e| (6, e))?;
    let started = super::stage::now();
    let result = driver.execute(session, prompt);
    if let Ok(head) = driver.head() {
        super::stage::record(record, "writing", &head, started);
    }
    record["writer_turns"][index]["ended"] = json!(chrono::Utc::now().to_rfc3339());
    driver.save(record).map_err(|e| (6, e))?;
    result.map_err(|e| {
        let code = if e.starts_with(pg_worker_core::host_readiness::SANDBOX_UNAVAILABLE) {
            2
        } else {
            6
        };
        (code, e)
    })
}
