//! Exact full-diff bindings for review dispatch and completion.
use super::{git, json, parameter, Path, ToolError, Value};
use pg_worker_core::{run::record::RunRecord, workspace_resolver};
use sha2::{Digest, Sha256};
use std::{process::Stdio, time::Duration};

#[cfg_attr(test, derive(Debug))]
pub(crate) struct Dispatch {
    pub head: String,
    pub binding: Option<Value>,
}

impl Dispatch {
    pub fn new(cwd: &Path, head: String, reference: &Value) -> Result<Self, ToolError> {
        let binding = reference
            .get("base_sha")
            .or_else(|| reference.get("base"))
            .filter(|_| reference["paths"].is_null())
            .map(|base| {
                let base = base
                    .as_str()
                    .ok_or_else(|| parameter("invalid review base"))?;
                let base = git(
                    cwd,
                    &[
                        "rev-parse",
                        "--verify",
                        "--end-of-options",
                        &format!("{base}^{{commit}}"),
                    ],
                )?;
                binding(cwd, &base, &head)
            })
            .transpose()?;
        Ok(Self { head, binding })
    }

    pub fn apply(&self, metadata: &mut Value) {
        metadata["head_sha"] = json!(self.head);
        if let Some(binding) = &self.binding {
            metadata["review_base"] = binding["base_sha"].clone();
            metadata["review_binding"] = binding.clone();
        }
    }
}

pub(super) fn matches(cwd: &Path, head: &str, expected: &Value) -> bool {
    expected["head_sha"].as_str() == Some(head)
        && expected["base_sha"]
            .as_str()
            .and_then(|base| binding(cwd, base, head).ok())
            .as_ref()
            == Some(expected)
}

pub(crate) fn completed(record: &RunRecord) -> bool {
    record
        .get("review_binding")
        .filter(|value| !value.is_null())
        .is_some_and(|binding| {
            record
                .get("completion")
                .and_then(|completion| completion.get("review_binding"))
                == Some(binding)
        })
}

fn binding(cwd: &Path, base: &str, head: &str) -> Result<Value, ToolError> {
    let invalid = || parameter("review diff unavailable");
    let is_sha = |value: &str| value.len() == 40 && value.bytes().all(|b| b.is_ascii_hexdigit());
    if !is_sha(base) || !is_sha(head) {
        return Err(invalid());
    }
    let mut command = workspace_resolver::git_command(cwd);
    command
        .args([
            "--no-replace-objects",
            "diff",
            "--raw",
            "--no-color",
            "--no-renames",
            "--no-relative",
            "--no-ext-diff",
            "--no-textconv",
            "--ignore-submodules=none",
            "--abbrev=40",
            "-z",
            base,
            head,
            "--",
        ])
        .stdout(Stdio::piped())
        .stderr(Stdio::null());
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        command.process_group(0);
    }
    let diff = crate::prompt_git::collect_diff(
        command.spawn().map_err(|_| invalid())?,
        16 * 1024 * 1024,
        Duration::from_secs(10),
    )
    .map_err(|_| invalid())?;
    // Raw records bind the path, old and new object IDs and file modes. Path
    // ordering in Git can depend on the user's diff.orderFile.
    let mut changes = Vec::new();
    if !diff.is_empty() {
        let records = diff.strip_suffix(&[0]).ok_or_else(invalid)?;
        let mut fields = records.split(|byte| *byte == 0);
        while let Some(change) = fields.next() {
            let path = fields.next().ok_or_else(invalid)?;
            changes.push((path, change));
        }
    }
    changes.sort_unstable();
    let mut digest = Sha256::new();
    for (path, change) in changes {
        digest.update(change);
        digest.update([0]);
        digest.update(path);
        digest.update([0]);
    }
    Ok(json!({"base_sha":base,"head_sha":head,"diff_sha256":format!("{:x}", digest.finalize())}))
}
