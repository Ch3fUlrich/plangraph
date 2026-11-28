//! `land-train deploy-train --base SHA --merge SHA --train-head SHA [--repo DIR] [--state-dir DIR]`: the
//! deploy step of a train settlement (TRAIN-SETTLE-RUNS-DEPLOY-APPLY), run after the merge is verified and
//! before the train's items close.
//!
//! It plans the landed range (`merge^1..merge` when the merge is a commit of its own, else `base..merge`),
//! saves the plan beside its state under the state directory and runs `deploy-apply` on it, so a rerun
//! derives the same plan and resumes from the same state. `PG_LAND_DEPLOY=apply` installs, with
//! `PG_LAND_DEPLOY_HOSTS` (the host registry), `PG_LAND_DEPLOY_ROLE_HOSTS` (`host=role+role,...`, else the
//! registry's `PG_HOST_ROLES`) and `PG_LAND_DEPLOY_ROW_CMD` (the deploy row command); a failed first run
//! fails the step. `PG_LAND_DEPLOY=off` skips it. Anything else is the dry run, which reports a failure
//! but never fails the settlement.
use std::collections::BTreeMap;
use std::path::PathBuf;

/// Where the plans and states of train deploys live.
fn default_state_dir() -> PathBuf {
    std::env::var_os("XDG_STATE_HOME")
        .filter(|v| !v.is_empty())
        .map(PathBuf::from)
        .or_else(|| std::env::var_os("HOME").map(|h| PathBuf::from(h).join(".local/state")))
        .unwrap_or_default()
        .join("plangraph/train-deploy")
}

type Flags = BTreeMap<String, String>;

/// The flags of the plan and the apply of one train deploy; `env` reads a setting.
pub fn step_flags(
    flags: &Flags,
    apply: bool,
    env: &dyn Fn(&str) -> Option<String>,
) -> Result<(Flags, Flags), String> {
    let need = |k: &str| {
        flags
            .get(k)
            .cloned()
            .ok_or_else(|| format!("deploy-train: missing --{k}"))
    };
    let (merge, head) = (need("merge")?, need("train-head")?);
    let base = if merge == head {
        need("base")?
    } else {
        format!("{merge}^1")
    };
    let dir = flags
        .get("state-dir")
        .map(PathBuf::from)
        .unwrap_or_else(default_state_dir);
    let file = |ext: &str| {
        dir.join(format!("{merge}.{ext}"))
            .to_string_lossy()
            .into_owned()
    };
    let repo = flags.get("repo").cloned().unwrap_or_else(|| ".".into());
    let mut plan = BTreeMap::from([
        ("repo".to_owned(), repo.clone()),
        ("base".to_owned(), base),
        ("head".to_owned(), merge.clone()),
        ("plan-out".to_owned(), file("plan.json")),
    ]);
    let mode = if apply { "apply" } else { "dry-run" };
    let mut run = BTreeMap::from([
        ("plan".to_owned(), file("plan.json")),
        ("state".to_owned(), file("state.json")),
        ("landed-head".to_owned(), merge),
        ("repo".to_owned(), repo),
        ("mode".to_owned(), mode.to_owned()),
    ]);
    if let Some(hosts) = env("PG_LAND_DEPLOY_HOSTS") {
        plan.insert("hosts".into(), hosts.clone());
        run.insert("hosts".into(), hosts);
    }
    if let Some(roles) = env("PG_LAND_DEPLOY_ROLE_HOSTS") {
        plan.insert("role-hosts".into(), roles);
    }
    if let Some(cmd) = env("PG_LAND_DEPLOY_ROW_CMD") {
        run.insert("row-cmd".into(), cmd);
    }
    Ok((plan, run))
}

pub(super) fn run_cli(flags: &BTreeMap<String, String>) -> Result<Vec<String>, String> {
    let env = |k: &str| std::env::var(k).ok().filter(|v| !v.trim().is_empty());
    let mode = env("PG_LAND_DEPLOY").unwrap_or_default();
    if mode == "off" {
        return Ok(vec!["deploy: off (PG_LAND_DEPLOY=off)".into()]);
    }
    let apply = mode == "apply";
    let result = step_flags(flags, apply, &env).and_then(|(plan, run)| {
        if let Some(dir) = PathBuf::from(&run["state"]).parent() {
            std::fs::create_dir_all(dir).map_err(|e| format!("{}: {e}", dir.display()))?;
        }
        let mut lines = super::deploy_plan_cli::run_cli(&plan)?;
        lines.extend(super::deploy_apply::run_cli(&run)?);
        Ok(lines)
    });
    match result {
        Err(e) if !apply => Ok(vec![format!(
            "deploy dry run failed (settlement goes on): {e}"
        )]),
        other => other,
    }
}
