//! The `MIGRATION` field of the GO row of one pull request and head, read for the evidence.
//!
//! One parser serves both landing paths: `store_waiver.migration_for_go` (the keeper's GO binding and the
//! plain-decimal field grammar the batch land gate reads), run from the copy of that file built into this
//! binary through [`super::py_reader`].
use super::py_reader;
use std::process::Command;

const SOURCE: &str = include_str!("../../../../src/plangraph/keeper/store_waiver.py");
const NAMES: [&str; 8] = [
    "GO_PREFIX",
    "_GO_RULING",
    "_GO_HEAD",
    "_strip_prefix",
    "_go_statement",
    "_go_binding",
    "migration_field",
    "migration_for_go",
];

/// The `MIGRATION` version of `decision` (a GO decision cell or statement) for this exact pull request and
/// head; `None` when it has none. An error when the decision is bound to another pull request or head, names
/// no single GO-HEAD, or has a repeated or malformed field.
pub fn migration_for(
    python: &dyn Fn() -> Command,
    decision: &str,
    repo: &str,
    pr: u64,
    head: &str,
) -> Result<Option<u32>, String> {
    let value = py_reader::call(
        python,
        SOURCE,
        &NAMES,
        "migration_for_go",
        serde_json::json!([decision, repo, pr, head]),
    )?;
    match value {
        serde_json::Value::Null => Ok(None),
        other => other
            .as_u64()
            .and_then(|v| u32::try_from(v).ok())
            .map(Some)
            .ok_or_else(|| format!("the GO reader printed {other}")),
    }
}
