//! Native writer review routing delegates to the host spawner before opening a seat.
use super::Host;
use serde_json::{json, Value};
use std::path::Path;

impl Host {
    /// A structured empty route is eligible for deferral. Command and admission
    /// failures still enter the normal no-verdict retry path.
    pub(super) fn review_preflight(
        &self,
        clone: &Path,
        card: &Value,
        families: &[String],
        head: &str,
    ) -> Result<Option<Value>, String> {
        let output = super::output(
            self.command(&self.review_cli[0])
                .args(&self.review_cli[1..])
                .arg("--state")
                .arg(&self.state)
                .args(["route", "--card", &card.to_string(), "--role", "review"])
                .args(
                    families
                        .iter()
                        .flat_map(|family| ["--writer-family", family]),
                )
                .arg("--json")
                .current_dir(clone),
        )?;
        let plan: Value = serde_json::from_str(&output)
            .map_err(|error| format!("review route preview unreadable: {error}"))?;
        let legs = plan["legs"]
            .as_array()
            .ok_or("review route preview did not report legs")?;
        if !legs.is_empty() {
            return Ok(None);
        }
        let causes = plan["skipped"]
            .as_array()
            .filter(|causes| !causes.is_empty())
            .ok_or("empty review route has no per-leg causes")?;
        Ok(Some(
            json!({"status":"deferred_to_batch_seat","head":head,"causes":causes}),
        ))
    }
}
/// Optional card pin with the same audited reason bar as other explicit legs.
pub(super) fn review_override(card: &Value) -> Result<Option<(String, String)>, String> {
    let (leg, reason) = (&card["review_leg"], &card["review_override_reason"]);
    if leg.is_null() && reason.is_null() {
        return Ok(None);
    }
    let leg = leg
        .as_str()
        .filter(|leg| {
            leg.starts_with(|c: char| c.is_ascii_alphanumeric())
                && leg
                    .bytes()
                    .all(|c| c.is_ascii_alphanumeric() || b"-._".contains(&c))
        })
        .ok_or("review_leg_invalid: expected a registered leg ID")?;
    let reason = reason
        .as_str()
        .map(str::trim)
        .filter(|reason| reason.chars().count() >= 10)
        .ok_or("review_leg_needs_reason: card.review_override_reason must contain at least 10 characters")?;
    Ok(Some((leg.into(), reason.into())))
}
