//! Low-trust CLI review provenance (rule 3.26), bound to a checked repository head.
use crate::{
    errors::ToolError,
    launch::LaunchSpec,
    placement::{parameter, Args},
};
use pg_worker_core::run::record::{RunRecord, Status};
use serde_json::{json, Value};
use std::{path::Path, time::Duration};

pub(crate) fn kind(args: &Args) -> Option<&str> {
    args.get("leg")
        .and_then(Value::as_str)
        .filter(|s| ["agy-cli", "claude-cli"].contains(s))
}
pub(crate) fn eligible(leg: &Value) -> bool {
    matches!(
        (leg["harness"].as_str(), leg["model"].as_str()),
        (
            Some("agy"),
            Some("gemini-4-hydrogen-high" | "gemini-4-argon-medium")
        ) | (Some("claude"), Some("claude-sonnet-5-5-high"))
    )
}
pub(crate) fn select(args: &Args, text: String) -> Result<String, ToolError> {
    let Some(kind) = kind(args) else {
        return Ok(text);
    };
    let mut plan: Value =
        serde_json::from_str(&text).map_err(|_| parameter("invalid review route"))?;
    let harness = if kind == "agy-cli" { "agy" } else { "claude" };
    let legs: Vec<_> = plan["legs"]
        .as_array()
        .ok_or_else(|| parameter("invalid review route"))?
        .iter()
        .filter(|l| eligible(l) && l["harness"] == harness)
        .cloned()
        .collect();
    plan["first_leg"] = legs
        .first()
        .cloned()
        .ok_or_else(|| parameter("no admitted CLI review leg"))?;
    plan["legs"] = json!(legs);
    Ok(plan.to_string())
}
pub(crate) fn cwd(
    args: &Args,
    fallback: std::path::PathBuf,
) -> Result<std::path::PathBuf, ToolError> {
    let requested = args["card"]
        .get("cwd")
        .or_else(|| args["card"]["diff_ref"].get("repo"));
    let Some(requested) = requested else {
        return Ok(fallback);
    };
    let path = requested
        .as_str()
        .map(std::path::PathBuf::from)
        .filter(|p| p.is_absolute() && p.is_dir())
        .ok_or_else(|| parameter("CLI review cwd must be an existing absolute checkout"))?;
    Ok(path)
}

fn git(cwd: &Path, args: &[&str]) -> Result<String, ToolError> {
    let result = pg_worker_core::workspace_resolver::git_command(cwd)
        .args(args)
        .output()
        .map_err(|_| parameter("review repository unavailable"))?;
    if !result.status.success() {
        return Err(parameter("review repository unavailable"));
    }
    String::from_utf8(result.stdout)
        .map(|s| s.trim().into())
        .map_err(|_| parameter("invalid git output"))
}
fn clean_head(cwd: &Path) -> Result<String, ToolError> {
    if !git(cwd, &["status", "--porcelain", "--untracked-files=no"])?.is_empty() {
        return Err(parameter("CLI review requires a clean worktree"));
    }
    git(cwd, &["rev-parse", "HEAD"])
}
pub(crate) fn prepare(
    args: &Args,
    launch: &mut LaunchSpec,
    leg: &Value,
) -> Result<Option<String>, ToolError> {
    let head = clean_head(&launch.cwd)?;
    let expected = args["card"]
        .get("head_sha")
        .or_else(|| args["card"]["diff_ref"].get("head_sha"));
    if expected.is_some_and(|v| v.as_str() != Some(head.as_str())) {
        return Err(parameter("CLI review head does not match worktree"));
    }
    if let Some(reference) = args["card"]["diff_ref"].get("head") {
        let reference = reference
            .as_str()
            .ok_or_else(|| parameter("invalid review head"))?;
        let resolved = git(
            &launch.cwd,
            &[
                "rev-parse",
                "--verify",
                "--end-of-options",
                &format!("{reference}^{{commit}}"),
            ],
        )?;
        if resolved != head {
            return Err(parameter("CLI review diff head does not match worktree"));
        }
    }
    let gitdir = git(&launch.cwd, &["rev-parse", "--absolute-git-dir"])?;
    if !Path::new(&gitdir).join("fast-checks").join(&head).is_file() {
        return Err(parameter("CLI review requires fast-checks stamp for HEAD"));
    }
    let old = launch
        .prompt
        .as_deref()
        .ok_or_else(|| parameter("missing review prompt"))?;
    let access = if leg["harness"] == "agy" {
        launch.argv.extend([
            "--settings".into(),
            json!({"permissions":{"allow":[
                "read_file(*)", "list_dir(*)", "grep_search(*)", "find_by_name(*)"
            ]}})
            .to_string(),
        ]);
        "Use read_file, list_dir, grep_search and find_by_name for repository access. Do not request command tools. "
    } else {
        ""
    };
    let prompt = format!("{access}Read-only repository review of HEAD {head}. Open callers and tests; never edit. End with exactly VERDICT: PASS or VERDICT: FAIL, then TEST_GAMING: no or TEST_GAMING: yes. Report findings before the verdict.\n\n{old}");
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
    Ok(Some(head))
}

fn read_log(record: &RunRecord) -> std::io::Result<String> {
    let path = record.path().with_file_name("live.log");
    if std::fs::metadata(&path)?.len() > 8 * 1024 * 1024 {
        return Err(std::io::Error::other("review log too large"));
    }
    std::fs::read_to_string(path)
}

pub(crate) fn finish(record: &mut RunRecord, status: Status, leg: &Value) {
    if record.get("role").and_then(Value::as_str) != Some("review")
        || !eligible(leg)
        || status == Status::DryRun
    {
        return;
    }
    let value = record.to_value();
    let Some(state) = record
        .path()
        .parent()
        .and_then(Path::parent)
        .and_then(Path::parent)
    else {
        return;
    };
    let text = crate::review_artifacts::review_text(state, &value).unwrap_or_default();
    // Book provider errors through the existing store/parser, never answer prose.
    if status != Status::Completed || text.is_empty() || text.contains("VERDICT: HALT") {
        if let Ok(log) = read_log(record) {
            let mut argv = vec![
                "quota-result".into(),
                "--model".into(),
                leg["model"].as_str().unwrap().into(),
            ];
            let mut errors: Vec<_> = log
                .lines()
                .filter_map(|l| serde_json::from_str::<Value>(l).ok())
                .filter(|v| v["event"] == "result" && v["result"]["status"] != "SUCCESS")
                .filter_map(|v| v["result"]["error"].as_str().map(str::to_owned))
                .collect();
            if let Some(stderr) = value["stderr_tail"].as_str().filter(|s| !s.is_empty()) {
                errors.push(stderr.to_owned());
            }
            if !errors.is_empty() && leg["harness"] == "agy" {
                argv.extend(["--error".into(), errors.join("\n")]);
                if !matches!(
                    crate::adapter::Adapter(state).run(&argv, Duration::from_secs(10)),
                    Ok((0, _, _))
                ) {
                    record.set("quota_record_error", json!(true));
                }
            }
        }
        return;
    }
    let Some(head) = value["head_sha"].as_str() else {
        return;
    };
    let Some(cwd) = value["cwd"].as_str() else {
        return;
    };
    if clean_head(Path::new(cwd)).ok().as_deref() != Some(head) {
        return;
    }
    let mut model = leg["model"].as_str().unwrap().to_owned();
    if leg["harness"] == "claude" {
        let Ok(log) = read_log(record) else {
            return;
        };
        let mut models = std::collections::BTreeSet::new();
        for frame in log
            .lines()
            .filter_map(|l| serde_json::from_str::<Value>(l).ok())
        {
            let reported = if frame["type"] == "assistant" {
                frame["message"]["model"].as_str()
            } else if frame["type"] == "system" && frame["subtype"] == "init" {
                frame["model"].as_str()
            } else {
                None
            };
            if let Some(m) = reported {
                models.insert(m.to_owned());
            }
        }
        if models.len() != 1 {
            return;
        }
        model = models.into_iter().next().unwrap();
        if !model.starts_with("claude-sonnet-5-5") {
            return;
        }
    }
    let verdicts: Vec<_> = text
        .lines()
        .filter_map(|l| l.strip_prefix("VERDICT: "))
        .collect();
    let verdict = match verdicts.as_slice() {
        ["PASS"] => "pass",
        ["FAIL"] => "fail",
        _ => return,
    };
    let gaming: Vec<_> = text
        .lines()
        .filter_map(|l| l.strip_prefix("TEST_GAMING: "))
        .collect();
    record.set("served_model", json!(model));
    record.set(
        "served_family",
        json!(if leg["harness"] == "agy" {
            "google"
        } else {
            "anthropic"
        }),
    );
    record.set("witness_kind", json!("cli_reported"));
    record.set(
        "served_witness_reason",
        json!("CLI model selection/report; no authenticated provider witness (rule 3.26)"),
    );
    record.set("stdout_tail", json!(text));
    record.set("verdict", json!(verdict));
    record.set(
        "findings",
        json!(text
            .lines()
            .take_while(|l| !l.starts_with("VERDICT:"))
            .collect::<Vec<_>>()
            .join("\n")),
    );
    record.set("test_gaming", json!(gaming.as_slice() != ["no"]));
    record.set("finish_reason", json!("stop"));
    record.set(
        "completion",
        json!({"head_sha":head,"finish_reason":"stop"}),
    );
}
#[cfg(test)]
#[path = "cli_review_tests.rs"]
mod tests;
