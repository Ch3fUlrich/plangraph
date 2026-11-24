//! Include vetted writer-host evidence in standalone and deferred batch review prompts.
use crate::fast_checks::ReplayReceipt;
use crate::review_git::ResolvedRange;
use pg_worker_core::{
    run::record::run_dir, workspace_resolver::git_command, worktree::fast_check_stamp,
};
use serde_json::{json, Value};
use std::{fs, path::Path};

pub(super) struct Evidence(Vec<ReplayReceipt>);

impl Evidence {
    pub(super) fn load(
        range: &ResolvedRange,
        state: Option<&Path>,
        native_parts: &[String],
    ) -> Result<Self, String> {
        let mut receipts = Vec::new();
        if let Ok(stamp) = fast_check_stamp(&range.repo_root, &range.head_sha) {
            if let Ok(receipt) = ReplayReceipt::read(&stamp) {
                if receipt.matches(&range.base_sha, &range.head_sha) {
                    receipts.push(receipt);
                }
            }
        }
        for part in native_parts {
            let state = state.ok_or("native part replay requires the writer's spawner state")?;
            let dir = run_dir(state, part).map_err(|error| error.to_string())?;
            let bytes = fs::read(dir.join("record.json"))
                .map_err(|error| format!("native part {part}: {error}"))?;
            let part_record: Value = serde_json::from_slice(&bytes)
                .map_err(|error| format!("native part {part}: {error}"))?;
            receipts.push(part_receipt(range, part, &part_record)?);
        }
        receipts.sort_by(|a, b| a.base.cmp(&b.base).then_with(|| a.head.cmp(&b.head)));
        receipts.dedup_by(|a, b| a.base == b.base && a.head == b.head);
        Ok(Self(receipts))
    }

    pub(super) fn add(&self, card: &mut Value, prompt: &mut String) {
        if !self.0.is_empty() {
            card["writer_test_replays"] = json!(self.0);
            for receipt in &self.0 {
                prompt.push('\n');
                prompt.push_str(&receipt.evidence());
            }
        }
    }
}

fn ancestor(repo: &Path, base: &str, head: &str) -> Result<bool, String> {
    match git_command(repo)
        .args(["merge-base", "--is-ancestor", base, head])
        .output()
        .map_err(|error| error.to_string())?
        .status
        .code()
    {
        Some(0) => Ok(true),
        Some(1) => Ok(false),
        _ => Err("cannot confirm native part ancestry".into()),
    }
}

fn part_receipt(
    range: &ResolvedRange,
    part: &str,
    record: &Value,
) -> Result<ReplayReceipt, String> {
    let mismatch = || format!("native part {part} has no matching deferred replay receipt");
    let checks = &record["fast_checks"];
    let basis = &record["review_base"];
    let handoff = &record["deferred_review"];
    let receipt: ReplayReceipt =
        serde_json::from_value(handoff["test_replay"].clone()).map_err(|_| mismatch())?;
    if record["run_id"] != part
        || record["run_kind"] != "native-codex"
        || record["role"] != "implement"
        || record["status"] != "completed"
        || record["review_status"] != "deferred_to_batch_seat"
        || record["exit_code"] != 0
        || checks["status"] != "passed"
        || checks["replay"] != handoff["test_replay"]
        || basis["base"] != receipt.base
        || checks["base"] != receipt.base
        || record["base_sha"] != receipt.base
        || basis["head"] != receipt.head
        || record["final_head"] != receipt.head
        || handoff["head"] != receipt.head
        || checks["head"] != receipt.head
        || !receipt.matches(&receipt.base, &receipt.head)
    {
        return Err(mismatch());
    }
    let (base, head, repo) = (&receipt.base, &receipt.head, &range.repo_root);
    if base == head
        || !ancestor(repo, base, head)?
        || !ancestor(repo, head, &range.head_sha)?
        || ancestor(repo, head, &range.base_sha)?
    {
        return Err(format!("native part {part} is outside the reviewed range"));
    }
    Ok(receipt)
}
