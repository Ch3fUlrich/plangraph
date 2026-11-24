//! Check a router or train receipt against the Keeper's trusted run records.
use pg_worker_core::counted_seat::{Binding, Policy};
use std::process::Stdio;

pub(crate) fn require(
    policy: &Policy,
    key: &str,
    head: &str,
    run: &str,
    expected: Option<&Binding>,
) -> Result<Binding, String> {
    let code = r#"
import json, os, sys
from pathlib import Path
from plangraph.keeper.config import load_config
from plangraph.keeper.evidence_spawner import SpawnerRecordAdapter
from plangraph.keeper.store_merge_locks import require_train_review
config = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "plangraph" / "keeper.json"
adapter = SpawnerRecordAdapter(load_config(config).spawner_records_dir)
print(json.dumps(require_train_review(adapter, *sys.argv[1:])))
"#;
    // An unverified worktree cannot supply the landing verifier's Python package.
    let output = pg_worker_core::writer_families::python_command(None)
        .args(["-I", "-c", code, key, head, run])
        .stdin(Stdio::null())
        .stderr(Stdio::null())
        .output()
        .map_err(|_| "counted review mirror could not be read")?;
    if !output.status.success() {
        return Err(format!(
            "counted review {run} is not a passing seat for {head}"
        ));
    }
    let row: serde_json::Value = serde_json::from_slice(&output.stdout)
        .map_err(|_| "invalid counted review mirror response")?;
    let binding: Binding = serde_json::from_value(row["counted_review"].clone())
        .map_err(|_| "missing counted review mirror binding")?;
    policy.verify(&binding, head, run, row["leg"].as_str().unwrap_or(""))?;
    if binding.item_key.as_deref() != Some(key) || expected.is_some_and(|record| record != &binding)
    {
        return Err("counted review differs from the registered receipt".into());
    }
    Ok(binding)
}
