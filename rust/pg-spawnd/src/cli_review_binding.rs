//! Exact full-diff bindings for review dispatch and completion.
use super::{git, json, parameter, LaunchSpec, Path, ToolError, Value};
use pg_worker_core::{run::record::RunRecord, workspace_resolver};
use sha2::{Digest, Sha256};
use std::{
    process::{Command, Stdio},
    time::Duration,
};

#[cfg_attr(test, derive(Debug))]
pub(crate) struct Dispatch {
    pub head: String,
    pub binding: Option<Value>,
    /// The merge-base diff and path subset of a seat that has no full-diff binding (an inline
    /// seat), recorded at dispatch and re-verified by the classifier at completion.
    pub scope: Option<Value>,
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
        let scope = if binding.is_none() {
            scope(cwd, &head, reference)
        } else {
            None
        };
        Ok(Self {
            head,
            binding,
            scope,
        })
    }

    pub fn apply(&self, metadata: &mut Value) {
        metadata["head_sha"] = json!(self.head);
        if let Some(binding) = &self.binding {
            metadata["review_base"] = binding["base_sha"].clone();
            metadata["review_binding"] = binding.clone();
        }
        if let Some(scope) = &self.scope {
            metadata["review_scope"] = scope.clone();
        }
    }

    pub fn prompt(
        &self,
        launch: &LaunchSpec,
        leg: &Value,
        access: &str,
        original: &str,
    ) -> Result<String, ToolError> {
        let head = &self.head;
        let base = self
            .binding
            .as_ref()
            .and_then(|binding| binding["base_sha"].as_str());
        let inline_diff = leg["harness"] == "claude";
        let range = base.filter(|_| !inline_diff).map_or_else(String::new, |base| {
            format!(" Review the full diff using `git --no-replace-objects diff --no-ext-diff --no-textconv {base} {head} --`.")
        });
        let prompt = format!("{access}Read-only repository review of HEAD {head}.{range} Open callers and tests; never edit. Do not run background commands; wait for every command to finish before deciding. Name at least one reviewed path from the diff or a file:line in your review. End with exactly VERDICT: PASS or VERDICT: FAIL, then TEST_GAMING: no or TEST_GAMING: yes. Report findings before the verdict.\n\n{original}");
        let Some(base) = base.filter(|_| inline_diff) else {
            return Ok(prompt);
        };
        // Sonnet's Bash policy is scoped to tests. Supply the bound diff directly.
        let child = diff_command(&launch.cwd)
            .args(["--patch", "--submodule=short", base, head, "--"])
            .spawn()
            .map_err(|_| parameter("review diff unavailable"))?;
        let cap = crate::prompt::diff_cap(leg);
        let patch = crate::prompt_git::collect_diff(child, cap, Duration::from_secs(10)).map_err(
            |err| match err {
                crate::prompt::InlineDiffError::TooLarge => parameter(
                    crate::prompt::oversized_reason(
                        cap,
                        crate::prompt::leg_max_request_tokens(leg),
                        None,
                    )
                    .to_string(),
                ),
                _ => parameter("review diff unavailable"),
            },
        )?;
        let payload = crate::prompt::escape_payload(&patch);
        let prompt = crate::prompt::format_review_prompt(&prompt, false, None, &payload);
        crate::prompt::check_prompt_limit(&prompt, leg)
            .map_err(|err| parameter(err.to_string()))?;
        Ok(prompt)
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

fn diff_command(cwd: &Path) -> Command {
    let mut command = workspace_resolver::git_command(cwd);
    command
        .args([
            "--no-replace-objects",
            "diff",
            "--no-color",
            "--no-renames",
            "--no-relative",
            "--no-ext-diff",
            "--no-textconv",
            "--ignore-submodules=none",
        ])
        .stdout(Stdio::piped())
        .stderr(Stdio::null());
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        command.process_group(0);
    }
    command
}

/// The files of `base...head` limited to the card's path subset, with the digest of their sorted
/// names (`plangraph.review.verdict.diff_scope` recomputes it): the diff an inline seat is shown.
fn scope(cwd: &Path, head: &str, reference: &Value) -> Option<Value> {
    let is_sha = |value: &str| value.len() == 40 && value.bytes().all(|b| b.is_ascii_hexdigit());
    let base = reference
        .get("base_sha")
        .or_else(|| reference.get("base"))?
        .as_str()?;
    let base = git(
        cwd,
        &[
            "rev-parse",
            "--verify",
            "--end-of-options",
            &format!("{base}^{{commit}}"),
        ],
    )
    .ok()?;
    if !is_sha(&base) || !is_sha(head) {
        return None;
    }
    let paths: Option<Vec<String>> = reference["paths"].as_array().map(|paths| {
        paths
            .iter()
            .filter_map(|path| path.as_str().map(str::to_owned))
            .collect()
    });
    let child = diff_command(cwd)
        .args(["--name-only", "-z", &format!("{base}...{head}"), "--"])
        .args(
            paths
                .iter()
                .flatten()
                .map(|path| format!(":(literal){path}")),
        )
        .spawn()
        .ok()?;
    let names =
        crate::prompt_git::collect_diff(child, 16 * 1024 * 1024, Duration::from_secs(10)).ok()?;
    let mut files: Vec<&[u8]> = names
        .split(|byte| *byte == 0)
        .filter(|n| !n.is_empty())
        .collect();
    files.sort_unstable();
    let mut digest = Sha256::new();
    for file in files {
        digest.update(file);
        digest.update([0]);
    }
    Some(
        json!({"mode": "merge-base", "base": base, "head": head, "paths": paths,
                "files_sha256": format!("{:x}", digest.finalize())}),
    )
}

fn binding(cwd: &Path, base: &str, head: &str) -> Result<Value, ToolError> {
    let invalid = || parameter("review diff unavailable");
    let is_sha = |value: &str| value.len() == 40 && value.bytes().all(|b| b.is_ascii_hexdigit());
    if !is_sha(base) || !is_sha(head) {
        return Err(invalid());
    }
    let child = diff_command(cwd)
        .args(["--raw", "--abbrev=40", "-z", base, head, "--"])
        .spawn()
        .map_err(|_| invalid())?;
    let diff = crate::prompt_git::collect_diff(child, 16 * 1024 * 1024, Duration::from_secs(10))
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
