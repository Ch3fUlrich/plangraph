//! Bind a full checkout review to the same Git diff at dispatch and completion.
use super::{git, parameter, LaunchSpec, ToolError};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{path::Path, process::Stdio, time::Duration};

pub(super) const DIFF_ARGS: &[&str] = &[
    "--no-replace-objects",
    "diff",
    "--no-ext-diff",
    "--no-textconv",
    "--no-renames",
    "--no-color",
    "--full-index",
    "--binary",
];

fn diff_binding(cwd: &Path, base: &str, head: &str) -> Result<Value, ToolError> {
    for sha in [base, head] {
        if sha.len() != 40
            || !sha
                .bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
        {
            return Err(parameter("review diff binding requires full commit SHAs"));
        }
    }
    let unavailable = || parameter("review diff binding unavailable");
    let mut command = pg_worker_core::workspace_resolver::git_command(cwd);
    command
        .args(DIFF_ARGS)
        .args([base, head, "--"])
        .stdout(Stdio::piped())
        .stderr(Stdio::null());
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        command.process_group(0);
    }
    let child = command.spawn().map_err(|_| unavailable())?;
    let diff = crate::prompt_git::collect_diff(child, 64 * 1024 * 1024, Duration::from_secs(10))
        .map_err(|_| unavailable())?;
    Ok(
        json!({"base_sha":base, "head_sha":head, "diff_sha256":format!("{:x}", Sha256::digest(diff))}),
    )
}

/// Dispatch uses the original checkout to resolve aliases and the review checkout
/// to read the diff. The reviewer gets the exact Git command for that snapshot.
pub(crate) fn bind_launch(
    launch: &mut LaunchSpec,
    original_cwd: &Path,
    reference: &Value,
    metadata: &mut Value,
) -> Result<(), ToolError> {
    metadata["review_binding"] = Value::Null;
    let Some((head, base)) = metadata["head_sha"]
        .as_str()
        .zip(reference.get("base_sha").or_else(|| reference.get("base")))
    else {
        return Ok(());
    };
    if !reference["paths"].is_null() {
        return Ok(());
    }
    let base = base
        .as_str()
        .filter(|base| !base.is_empty())
        .ok_or_else(|| parameter("invalid review diff base"))?;
    let base = git(
        original_cwd,
        &[
            "--no-replace-objects",
            "rev-parse",
            "--verify",
            "--end-of-options",
            &format!("{base}^{{commit}}"),
        ],
    )?;
    let binding = diff_binding(&launch.cwd, &base, head)?;
    let old = launch
        .prompt
        .as_deref()
        .ok_or_else(|| parameter("missing review prompt"))?;
    let prompt = format!(
        "{old}\n\nReview the full pinned diff with `git {} {base} {head} --`. Use these commit IDs for the verdict.",
        DIFF_ARGS.join(" "),
    );
    replace_prompt(launch, prompt)?;
    metadata["review_base"] = binding["base_sha"].clone();
    metadata["review_binding"] = binding;
    Ok(())
}

pub(super) fn replace_prompt(launch: &mut LaunchSpec, prompt: String) -> Result<(), ToolError> {
    let old = launch
        .prompt
        .as_deref()
        .ok_or_else(|| parameter("missing review prompt"))?;
    if launch.stdin.as_deref() == Some(old) {
        launch.stdin = Some(prompt.clone());
    } else {
        let mut event: Value = serde_json::from_str(launch.stdin.as_deref().unwrap_or(""))
            .map_err(|_| parameter("invalid CLI prompt transport"))?;
        if event["event"] != "user" || event["message"]["content"] != old {
            return Err(parameter("invalid CLI prompt transport"));
        }
        event["message"]["content"] = json!(prompt);
        launch.stdin = Some(event.to_string() + "\n");
    }
    launch.prompt = Some(prompt);
    Ok(())
}

pub(super) fn matches_completion(record: &Value, cwd: &Path, head: &str) -> bool {
    record["review_base"]
        .as_str()
        .and_then(|base| diff_binding(cwd, base, head).ok())
        .is_some_and(|expected| expected == record["review_binding"])
}
