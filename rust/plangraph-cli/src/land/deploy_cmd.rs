//! The command boundary of the post-merge deploy step: the host and keeper commands it runs, and
//! `land-train deploy-apply`, which binds the step to the approved GO and the verified landed head.
use super::deploy::{DeployPlan, Target};
use super::deploy_derive::shipped_between;
use super::deploy_post::{
    apply, named_targets, DeployRow, Gate, Installer, PostState, RowSink, RunRead,
};
use super::git;
use std::collections::{BTreeMap, BTreeSet};
use std::io::Write;
use std::path::Path;
use std::process::{Command, Stdio};

/// Runs `argv... ACTION HOST NAME`: `install HOST FAMILY`, `read HOST FAMILY` (prints a [`RunRead`] as
/// JSON) and `disable HOST UNIT`. Exit 0 is success.
pub struct CommandInstaller {
    pub argv: Vec<String>,
}

impl CommandInstaller {
    fn run(&self, action: &str, host: &str, what: &str) -> Result<Vec<u8>, String> {
        let (cmd, args) = self
            .argv
            .split_first()
            .ok_or("no install command configured")?;
        let out = Command::new(cmd)
            .args(args)
            .args([action, host, what])
            .output()
            .map_err(|e| format!("{cmd} did not start: {e}"))?;
        if !out.status.success() {
            return Err(format!("{action} {what} on {host} exited {}", out.status));
        }
        Ok(out.stdout)
    }
}

impl Installer for CommandInstaller {
    fn install(&self, host: &str, family: &str) -> Result<(), String> {
        self.run("install", host, family).map(|_| ())
    }
    fn read_run(&self, host: &str, family: &str) -> Result<RunRead, String> {
        let out = self.run("read", host, family)?;
        serde_json::from_slice(&out).map_err(|e| format!("unreadable run report: {e}"))
    }
    fn disable(&self, host: &str, unit: &str) -> Result<(), String> {
        self.run("disable", host, unit).map(|_| ())
    }
}

/// Runs `argv... KEY` with the row as JSON on stdin; exit 0 means the keeper holds the row.
pub struct CommandRows {
    pub argv: Vec<String>,
}

impl RowSink for CommandRows {
    fn write(&self, row: &DeployRow) -> Result<(), String> {
        let (cmd, args) = self.argv.split_first().ok_or("no row command configured")?;
        let mut child = Command::new(cmd)
            .args(args)
            .arg(&row.key)
            .stdin(Stdio::piped())
            .spawn()
            .map_err(|e| format!("{cmd} did not start: {e}"))?;
        let body = serde_json::to_vec(row).map_err(|e| e.to_string())?;
        child
            .stdin
            .take()
            .ok_or("no stdin")?
            .write_all(&body)
            .map_err(|e| e.to_string())?;
        let status = child.wait().map_err(|e| e.to_string())?;
        status
            .success()
            .then_some(())
            .ok_or_else(|| format!("exited {status}"))
    }
}

fn wall_clock() -> i64 {
    chrono::Utc::now().timestamp()
}

/// Every target must be something the plan's train really ships: its family is in the net diff
/// `base..train`, a unit still exists in the landed tree, and each item key is a `Plan-Item` of the
/// train's commits.
fn check_in_tree(plan: &DeployPlan, repo: &Path, landed_head: &str) -> Result<(), String> {
    if plan.base.is_empty() {
        return Err("the deploy plan names no base (rerun deploy-plan --plan-out)".into());
    }
    let (families, items) = shipped_between(repo, &plan.base, &plan.train_sha)?;
    for t in &plan.targets {
        if !families.contains(&t.family) {
            return Err(format!(
                "{} is not shipped by train {}",
                t.family, plan.train_sha
            ));
        }
        if !t.family.starts_with("bin:")
            && git::out(
                repo,
                &[
                    "cat-file",
                    "-t",
                    &format!("{landed_head}:deploy/systemd/{}", t.family),
                ],
            )
            .is_err()
        {
            return Err(format!(
                "{} is absent from the landed tree {landed_head}",
                t.family
            ));
        }
        if let Some(k) = t.items.iter().find(|k| !items.contains(*k)) {
            return Err(format!(
                "item {k} of {} is not a Plan-Item of train {}",
                name_of(t),
                plan.train_sha
            ));
        }
    }
    Ok(())
}

fn name_of(t: &Target) -> String {
    format!("{}@{}", t.family, t.host)
}

/// The GO row, the landed head and the plan must belong together before anything is installed: the GO row
/// is the plan's own `deploy:` line, the plan's train is merged into the landed head, and the landed head is
/// on main. Returns the targets and the gate their first runs are verified against.
pub fn bind(
    plan: &DeployPlan,
    go_row: &str,
    repo: &Path,
    landed_head: &str,
    main_ref: &str,
    plan_digest: &str,
) -> Result<(Vec<Target>, Gate), String> {
    if plan.train_sha.is_empty() {
        return Err("the deploy plan names no train (rerun deploy-plan --plan-out)".into());
    }
    if plan.digest() != plan_digest.trim() {
        return Err("the plan does not match the approved plan digest".into());
    }
    if go_row.trim() != plan.go_req() {
        return Err(format!(
            "GO row {go_row:?} is not this plan's {:?}",
            plan.go_req()
        ));
    }
    if !git::is_ancestor(repo, &plan.train_sha, landed_head)? {
        return Err(format!(
            "train {} is not merged into landed head {landed_head}",
            plan.train_sha
        ));
    }
    if !git::is_ancestor(repo, landed_head, main_ref)? {
        return Err(format!("landed head {landed_head} is not on {main_ref}"));
    }
    check_in_tree(plan, repo, landed_head)?;
    let fresh_after = git::out(repo, &["show", "-s", "--format=%ct", landed_head])?
        .trim()
        .parse::<i64>()
        .map_err(|e| format!("commit time of {landed_head}: {e}"))?;
    let excluded: BTreeSet<(String, String)> = plan
        .blockers
        .iter()
        .filter_map(|b| Some((b.family.clone(), b.host.clone()?)))
        .collect();
    let gate = Gate {
        fresh_after,
        clock: wall_clock,
        excluded,
    };
    Ok((named_targets(plan, go_row)?, gate))
}

/// `land-train deploy-apply --plan FILE --go ROW --repo DIR --landed-head SHA [--main-ref REF] --plan-digest HEX
/// --state FILE --install-cmd CMD --row-cmd CMD`: the post-merge step. `--plan` is the `--plan-out` file of `deploy-plan`;
/// `--go` is the plan's `deploy:` line as approved; `--landed-head` is the verified merge on main; `--state`
/// is created or resumed and belongs to the plan's train.
pub(super) fn run_cli(flags: &BTreeMap<String, String>) -> Result<Vec<String>, String> {
    let need = |k: &str| flags.get(k).ok_or_else(|| format!("missing --{k}"));
    let words = |k: &str| -> Result<Vec<String>, String> {
        Ok(need(k)?.split_whitespace().map(str::to_owned).collect())
    };
    let plan_path = need("plan")?;
    let plan: DeployPlan =
        serde_json::from_slice(&std::fs::read(plan_path).map_err(|e| format!("{plan_path}: {e}"))?)
            .map_err(|e| format!("{plan_path}: {e}"))?;
    let main_ref = flags.get("main-ref").map_or("origin/main", String::as_str);
    let (targets, gate) = bind(
        &plan,
        need("go")?,
        Path::new(need("repo")?),
        need("landed-head")?,
        main_ref,
        need("plan-digest")?,
    )?;
    let state_path = Path::new(need("state")?);
    let mut state: PostState = match std::fs::read(state_path) {
        Ok(b) => {
            serde_json::from_slice(&b).map_err(|e| format!("{}: {e}", state_path.display()))?
        }
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => PostState::default(),
        Err(e) => return Err(format!("{}: {e}", state_path.display())),
    };
    if state.train.is_empty() {
        state.train = plan.train_sha.clone();
    } else if state.train != plan.train_sha {
        return Err(format!(
            "state {} belongs to train {}, not {}",
            state_path.display(),
            state.train,
            plan.train_sha
        ));
    }
    let installer = CommandInstaller {
        argv: words("install-cmd")?,
    };
    let rows = CommandRows {
        argv: words("row-cmd")?,
    };
    let mut save = |s: &PostState| {
        let text = serde_json::to_vec_pretty(s).map_err(|e| e.to_string())?;
        std::fs::write(state_path, text).map_err(|e| format!("{}: {e}", state_path.display()))
    };
    let report = apply(&targets, &gate, &installer, &rows, &mut state, &mut save)?;
    let mut lines = report.lines();
    if let Some((t, why)) = report.failed {
        return Err(format!("{}\nstop: {t}: {why}", lines.join("\n")));
    }
    if lines.is_empty() {
        lines.push("deploy: nothing to install".into());
    }
    Ok(lines)
}
