//! Review prompt safety, independent of tools-mode rollout.
use crate::launch::LaunchSpec;
use pg_worker_core::{
    hosts::{HostRegistry, RepoEntry},
    workspace::DiffRef,
};
use serde_json::Value;
use std::{collections::BTreeMap, path::PathBuf};

use pg_worker_core::review_split::{
    self as core_split, BEGIN_INLINE_DIFF as BEGIN, END_INLINE_DIFF as END,
};
pub(crate) use pg_worker_core::review_split::{
    diff_cap, escape_payload, format_review_prompt, leg_max_request_tokens,
};
#[derive(Debug, PartialEq, Eq)]
pub(crate) enum InlineDiffError {
    MissingDiffRef,
    InvalidDiffRef,
    TooLarge,
    Oversized(String),
    Git,
    UnsafeCarrier,
}
impl std::fmt::Display for InlineDiffError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::MissingDiffRef => f.write_str("missing diff_ref"),
            Self::InvalidDiffRef => f.write_str("invalid diff_ref"),
            Self::TooLarge => {
                f.write_str("inline_diff_failed: prompt_too_large: diff exceeds byte limit")
            }
            Self::Oversized(reason) => write!(f, "inline_diff_failed: {reason}"),
            Self::Git => f.write_str("inline diff git failed"),
            Self::UnsafeCarrier => f.write_str("unsafe prompt carrier"),
        }
    }
}
pub(crate) fn is_inline_only(leg: &Value) -> bool {
    leg["review_mode"] == "inline_only" || leg["tool_mode"] == "inline-only"
}
pub(crate) fn oversized_reason(
    cap: usize,
    max_tokens: Option<u64>,
    file: Option<&str>,
) -> InlineDiffError {
    InlineDiffError::Oversized(core_split::oversized_reason(cap, max_tokens, file))
}
pub(crate) fn check_prompt_limit(prompt: &str, leg: &Value) -> Result<(), InlineDiffError> {
    core_split::check_prompt_limit(prompt, leg).map_err(InlineDiffError::Oversized)
}
pub(crate) fn effective_prompt_leg<'a>(
    leg: &'a Value,
    launch: &LaunchSpec,
) -> std::borrow::Cow<'a, Value> {
    if let Some(prepared) = launch.max_request_tokens {
        let mut leg = leg.clone();
        let cap = leg_max_request_tokens(&leg).map_or(prepared, |routed| routed.min(prepared));
        if let Some(fields) = leg.as_object_mut() {
            fields.insert("max_request_tokens".into(), cap.into());
        }
        std::borrow::Cow::Owned(leg)
    } else {
        std::borrow::Cow::Borrowed(leg)
    }
}

pub(crate) fn check_prepared_prompt(
    launch: &LaunchSpec,
    text: &str,
) -> Result<(), crate::errors::ToolError> {
    let Some(max_request_tokens) = launch.max_request_tokens else {
        return Ok(());
    };
    check_prompt_limit(
        text,
        &serde_json::json!({ "max_request_tokens": max_request_tokens }),
    )
    .map_err(|e| crate::errors::ToolError::Refused {
        kind: crate::errors::RefusalKind::Spawn,
        message: e.to_string(),
        legs: vec![],
    })
}

pub(crate) fn config_root() -> PathBuf {
    std::env::var_os("XDG_CONFIG_HOME")
        .map(PathBuf::from)
        .or_else(|| {
            std::env::var_os(if cfg!(windows) { "APPDATA" } else { "HOME" }).map(|p| {
                if cfg!(windows) {
                    PathBuf::from(p)
                } else {
                    PathBuf::from(p).join(".config")
                }
            })
        })
        .unwrap_or_default()
        .join("plangraph")
}
pub(crate) fn repos() -> BTreeMap<String, RepoEntry> {
    HostRegistry::load(&config_root().join("hosts.json"))
        .map(|r| r.self_host().repos.clone())
        .unwrap_or_default()
}
pub(crate) fn mode(env: Option<&str>, config: &Value, role: &str) -> &'static str {
    match env
        .or_else(|| config["roles"][role]["inline_review_diff"].as_str())
        .or_else(|| config["inline_review_diff"].as_str())
        .unwrap_or("off")
    {
        "on" => "on",
        "shadow" => "shadow",
        _ => "off",
    }
}
pub(crate) fn configured_mode(role: &str) -> &'static str {
    use std::sync::{Mutex, OnceLock};
    use std::time::{Duration, Instant};
    static CACHE: OnceLock<Mutex<(Instant, Value)>> = OnceLock::new();
    let mut cache = CACHE
        .get_or_init(|| Mutex::new((Instant::now() - Duration::from_secs(2), Value::Null)))
        .lock()
        .unwrap();
    if cache.0.elapsed() >= Duration::from_secs(1) {
        cache.1 = std::fs::read(config_root().join("reliability.json"))
            .ok()
            .and_then(|b| serde_json::from_slice(&b).ok())
            .unwrap_or(Value::Null);
        cache.0 = Instant::now();
    }
    mode(
        std::env::var("PLANGRAPH_RELIABILITY_INLINE_REVIEW_DIFF")
            .ok()
            .as_deref(),
        &cache.1,
        role,
    )
}
/// Every attempt selects its review transport before interpreting diff_ref.
/// CLI reviews read a stamped checkout; inline reviews use registry-rooted SHAs.
pub(crate) fn prepare_launch(
    args: &crate::placement::Args,
    leg: &Value,
    launch: &mut LaunchSpec,
    dry: bool,
) -> Result<Option<crate::cli_review::Dispatch>, crate::errors::ToolError> {
    prepare_launch_group(args, leg, launch, dry, None)
}

pub(crate) fn prepare_launch_group(
    args: &crate::placement::Args,
    leg: &Value,
    launch: &mut LaunchSpec,
    dry: bool,
    group: Option<(usize, usize, &[String])>,
) -> Result<Option<crate::cli_review::Dispatch>, crate::errors::ToolError> {
    let role = args
        .get("role")
        .and_then(Value::as_str)
        .unwrap_or("implement");
    crate::pre_review::prepare(args, launch)?;
    if let Some(routed) = leg_max_request_tokens(leg) {
        launch.max_request_tokens = launch
            .max_request_tokens
            .map(|prepared| prepared.min(routed));
    }
    if let Some(text) = launch.prompt.as_deref() {
        check_prepared_prompt(launch, text)?;
    }
    if role != "review" {
        return Ok(None);
    }
    if crate::cli_review::eligible(leg) {
        return if dry {
            Ok(None)
        } else {
            let dispatch = crate::cli_review::prepare(args, launch, leg)?;
            if let Some(text) = launch.prompt.as_deref() {
                check_prepared_prompt(launch, text)?;
            }
            Ok(dispatch)
        };
    }
    let review_args = serde_json::json!(args);
    let mode = configured_mode(role);
    let repos = repos();
    if mode == "shadow" && !is_inline_only(leg) {
        let mut shadow = launch.clone();
        let would_inline = prepare_review(&review_args, leg, &mut shadow, &repos, true).is_ok()
            && shadow.prompt != launch.prompt;
        eprintln!(
            "inline_review_diff would_inline={}",
            usize::from(would_inline)
        );
    }
    match prepare_review_with_group(&review_args, leg, launch, &repos, mode == "on", group) {
        Ok(()) => Ok(None),
        Err(e @ (InlineDiffError::TooLarge | InlineDiffError::Oversized(_))) => {
            Err(crate::errors::ToolError::Refused {
                kind: crate::errors::RefusalKind::Spawn,
                message: e.to_string(),
                legs: vec![],
            })
        }
        Err(e) => Err(crate::errors::ToolError::Internal(e.to_string())),
    }
}
fn windows_guard(launch: &LaunchSpec, prompt: &str) -> Result<(), InlineDiffError> {
    if launch.argv.is_empty() {
        return Err(InlineDiffError::UnsafeCarrier);
    }
    if prompt.contains(['\r', '\n'])
        && launch.argv.iter().any(|arg| {
            let arg = arg.to_ascii_lowercase();
            arg.ends_with(".cmd") || arg.ends_with(".bat")
        })
    {
        return Err(InlineDiffError::UnsafeCarrier);
    }
    Ok(())
}
fn is_adapter_inlined(reference: &Value, leg: &Value, launch: &LaunchSpec, original: &str) -> bool {
    leg["review_mode"] != "inline_only"
        && reference.get("repo").is_some()
        && reference.get("repo_id").is_none()
        && launch.events.as_ref().is_some_and(|events| {
            events
                .iter()
                .any(|e| e.get("event").and_then(Value::as_str) == Some("launch_spec"))
        })
        && original.contains("\n\n=== INLINE DIFF ")
}

pub(crate) fn resolve_inline_diff_target<'a>(
    args: &Value,
    leg: &Value,
    launch: &LaunchSpec,
    repos: &'a BTreeMap<String, RepoEntry>,
    enabled: bool,
) -> Result<Option<(DiffRef, &'a RepoEntry, String, bool)>, InlineDiffError> {
    if args["role"] != "review" {
        return Ok(None);
    }
    let mandatory = is_inline_only(leg);
    let reference = &args["card"]["diff_ref"];
    if mandatory && reference.is_null() {
        return Err(InlineDiffError::MissingDiffRef);
    }
    if launch.prompt.is_none() && !mandatory && (reference.is_null() || !enabled) {
        return Ok(None);
    }
    let original = launch
        .prompt
        .as_deref()
        .ok_or(InlineDiffError::UnsafeCarrier)?;
    windows_guard(launch, original)?;
    if reference.is_null() {
        return if mandatory {
            Err(InlineDiffError::MissingDiffRef)
        } else {
            Ok(None)
        };
    }
    if !mandatory && !enabled {
        return Ok(None);
    }
    if is_adapter_inlined(reference, leg, launch, original) {
        check_prompt_limit(original, leg)?;
        return Ok(None);
    }
    let diff: DiffRef =
        serde_json::from_value(reference.clone()).map_err(|_| InlineDiffError::InvalidDiffRef)?;
    crate::prompt_git::validate(&diff)?;
    let repo = repos
        .get(&diff.repo_id)
        .ok_or(InlineDiffError::InvalidDiffRef)?;
    if launch.stdin.as_deref() != Some(original)
        || original.is_empty()
        || launch
            .argv
            .iter()
            .chain(launch.env.values())
            .any(|v| v.contains(original))
    {
        return Err(InlineDiffError::UnsafeCarrier);
    }
    let mut text = original.to_owned();
    let starts: Vec<_> = text.match_indices(BEGIN).map(|(i, _)| i).collect();
    let ends: Vec<_> = text.match_indices(END).map(|(i, _)| i).collect();
    match (starts.as_slice(), ends.as_slice()) {
        ([], []) => {}
        ([a], [b]) if a < b => {
            text.replace_range(*a..*b + END.len(), "");
        }
        _ => return Err(InlineDiffError::UnsafeCarrier),
    }
    Ok(Some((diff, repo, text, mandatory)))
}

pub(crate) fn prepare_review(
    args: &Value,
    leg: &Value,
    launch: &mut LaunchSpec,
    repos: &BTreeMap<String, RepoEntry>,
    enabled: bool,
) -> Result<(), InlineDiffError> {
    prepare_review_with_group(args, leg, launch, repos, enabled, None)
}

pub(crate) fn prepare_review_with_group(
    args: &Value,
    leg: &Value,
    launch: &mut LaunchSpec,
    repos: &BTreeMap<String, RepoEntry>,
    enabled: bool,
    group: Option<(usize, usize, &[String])>,
) -> Result<(), InlineDiffError> {
    let effective = effective_prompt_leg(leg, launch);
    let leg = effective.as_ref();
    let Some((mut diff, repo, text, mandatory)) =
        resolve_inline_diff_target(args, leg, launch, repos, enabled)?
    else {
        return Ok(());
    };
    if let Some((_, _, paths)) = group {
        diff.paths = Some(paths.to_vec());
    }
    let max_tokens = leg_max_request_tokens(leg);
    let cap = diff_cap(leg);
    let bytes = crate::prompt_git::read_diff(repo, &diff, cap).map_err(|err| match err {
        InlineDiffError::TooLarge => oversized_reason(cap, max_tokens, None),
        other => other,
    })?;
    let payload = escape_payload(&bytes);
    let banner =
        group.map(|(index, total, paths)| crate::review_split::group_banner(index, total, paths));
    let final_prompt = format_review_prompt(&text, mandatory, banner.as_deref(), &payload);
    check_prompt_limit(&final_prompt, leg)?;
    windows_guard(launch, &final_prompt)?;
    launch.stdin = Some(final_prompt.clone());
    launch.prompt = Some(final_prompt);
    Ok(())
}

#[cfg(test)]
#[path = "prompt_tests.rs"]
mod tests;
