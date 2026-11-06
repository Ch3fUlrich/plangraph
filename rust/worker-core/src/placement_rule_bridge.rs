//! Pure JSON boundary; authentication and durable transactions belong to adapters.
use crate::placement_rule_store::{
    validate_override, validate_snapshot, RuleEdit, RuleSnapshot, StoreError,
};
use crate::placement_rules::{resolve, schema, RuleLayers};
use serde::Deserialize;
use serde_json::{json, Value};
use std::collections::BTreeMap;

#[derive(Deserialize)]
#[serde(tag = "op", rename_all = "snake_case", deny_unknown_fields)]
enum Request {
    Specs {},
    Validate {
        edit: RuleEdit,
    },
    Report {
        snapshot: RuleSnapshot,
        host_class: String,
        host_id: String,
        process: BTreeMap<String, String>,
        env_file: BTreeMap<String, String>,
    },
}

pub fn handle(request: Value) -> Result<Value, StoreError> {
    let request = serde_json::from_value(request).map_err(|_| StoreError::Invalid)?;
    match request {
        Request::Specs {} => Ok(json!({"specs": schema().map_err(|_| StoreError::Invalid)?})),
        Request::Validate { edit } => {
            if edit.expected_revision > i64::MAX as u64
                || edit.rollback_of.is_some_and(|r| r > i64::MAX as u64)
            {
                return Err(StoreError::Invalid);
            }
            validate_override(&edit.scope, &edit.key, edit.value.as_deref())?;
            Ok(json!({}))
        }
        Request::Report {
            snapshot,
            host_class,
            host_id,
            process,
            env_file,
        } => {
            if host_id.trim().is_empty()
                || host_id.len() > 64
                || host_id.chars().any(char::is_control)
            {
                return Err(StoreError::Invalid);
            }
            validate_snapshot(&snapshot)?;
            let specs = schema().map_err(|_| StoreError::Invalid)?;
            let layers = RuleLayers {
                process,
                env_file,
                keeper_host: snapshot.hosts.get(&host_id).cloned().unwrap_or_default(),
                keeper_fleet: snapshot.fleet,
            };
            Ok(json!({"report": resolve(&specs, &host_class, &layers), "specs": specs}))
        }
    }
}

#[cfg(test)]
#[path = "placement_rule_bridge_tests.rs"]
mod tests;
