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
        let read_only =
            launch.events.iter().flatten().any(|event| {
                event.get("event").and_then(Value::as_str) == Some("sandbox_unavailable")
            });
        let range = base.filter(|_| !read_only).map_or_else(String::new, |base| {
            format!(" Review the full diff using `git --no-replace-objects diff --no-ext-diff --no-textconv {base} {head} --`.")
        });
        let prompt = format!("{access}Read-only repository review of HEAD {head}.{range} Open callers and tests; never edit. Do not run background commands; wait for every command to finish before deciding. Name at least one reviewed path from the diff or a file:line in your review. End with exactly VERDICT: PASS or VERDICT: FAIL, then TEST_GAMING: no or TEST_GAMING: yes. Report findings before the verdict.\n\n{original}");
        let Some(base) = base.filter(|_| read_only) else {
            return Ok(prompt);
        };
        // Use the same exact commit pair as the binding when the seat cannot run git.
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
