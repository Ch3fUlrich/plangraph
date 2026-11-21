//! Refuse an empty registry review before asking the router for a seat.
use crate::{
    errors::{RefusalKind, ToolError},
    placement::{parameter, Args},
};
use pg_worker_core::workspace::DiffRef;

pub(crate) fn check(args: &Args) -> Result<(), ToolError> {
    let reference = &args["card"]["diff_ref"];
    if args.get("role").is_none_or(|role| role != "review") || reference.get("repo_id").is_none() {
        return Ok(());
    }
    let diff: DiffRef = serde_json::from_value(reference.clone())
        .map_err(|_| parameter("invalid registry diff_ref"))?;
    let repos = crate::prompt::repos();
    let repo = repos
        .get(&diff.repo_id)
        .ok_or_else(|| parameter("unknown registry diff repository"))?;
    match crate::prompt_git::has_diff(repo, &diff) {
        Ok(true) => Ok(()),
        Ok(false) => Err(ToolError::Refused {
            kind: RefusalKind::Spawn,
            message: "empty review diff".into(),
            legs: vec![],
        }),
        Err(error) => Err(parameter(format!(
            "could not check registry review diff: {error}"
        ))),
    }
}
