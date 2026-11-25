//! Admission contract for `card.sandbox`; the same table as `refuse_card_sandbox` in
//! `plangraph.planner.route_policy`. An absent or null mode and `workspace-write` pass,
//! `danger-full-access` has its own reason, and every other value is refused without being echoed.
use serde_json::Value;

pub(crate) const REFUSED: &str = "sandbox_refused:";

pub(crate) fn refuse(card: &Value) -> Result<(), String> {
    match card.get("sandbox").filter(|mode| !mode.is_null()) {
        None => Ok(()),
        Some(Value::String(mode)) if mode == "workspace-write" => Ok(()),
        Some(Value::String(mode)) if mode == "danger-full-access" => Err(format!(
            "{REFUSED} danger-full-access is never granted to a spawned run; \
             only workspace-write is granted"
        )),
        Some(_) => Err(format!(
            "{REFUSED} card.sandbox is not a supported value; only workspace-write is granted"
        )),
    }
}

/// The refusal of a request's card as a tool error, before any leg, placement or binary lookup.
pub(crate) fn check(args: &crate::placement::Args) -> Result<(), crate::errors::ToolError> {
    args.get("card")
        .map_or(Ok(()), refuse)
        .map_err(crate::placement::parameter)
}

#[cfg(test)]
#[path = "card_sandbox_tests.rs"]
mod tests;
