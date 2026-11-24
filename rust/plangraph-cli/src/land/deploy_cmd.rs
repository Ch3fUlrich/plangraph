//! The command boundary of the post-merge deploy step: the host and keeper commands it runs, and
//! `land-train deploy-apply`, which binds the step to the approved GO and the verified landed head.
use super::deploy::DeployPlan;
use super::deploy_bind::{bind, Approved};
use super::deploy_post::{apply, DeployRow, Installer, PostState, RowSink, RunRead};
use super::go_verify::{verify, Evidence, Pins};
use std::collections::BTreeMap;
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
    fn install(&self, host: &str, family: &str) -> Result<Option<String>, String> {
        let out = self.run("install", host, family)?;
        let id = String::from_utf8_lossy(&out).trim().to_owned();
        Ok((!id.is_empty()).then_some(id))
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

/// The train the protected GO evidence approved: the evidence must verify with no refusal, and its head
/// is the train.
fn approved_train(path: &Path) -> Result<Approved, String> {
    let e: Evidence = serde_json::from_slice(
        &std::fs::read(path).map_err(|x| format!("{}: {x}", path.display()))?,
    )
    .map_err(|x| format!("{}: {x}", path.display()))?;
    let refusals = verify(&e, &Pins::protected()?);
    if !refusals.is_empty() {
        let lines: Vec<_> = refusals
            .iter()
            .map(|r| format!("{}: {}", r.code, r.detail))
            .collect();
        return Err(format!("the GO evidence is refused:\n{}", lines.join("\n")));
    }
    Ok(Approved {
        train_sha: e.head,
        base: e.base,
    })
}

/// `land-train deploy-apply --plan FILE --go ROW --repo DIR --landed-head SHA [--main-ref REF] --plan-digest HEX
/// --evidence FILE --state FILE --install-cmd CMD --row-cmd CMD`: the post-merge step. `--plan` is the `--plan-out` file of `deploy-plan`;
/// `--evidence` is the protected GO evidence of the approved train (it must verify, and its head and base
/// must be the plan's); `--go` is the plan's `deploy:` line as approved; `--landed-head` is the verified merge on main; `--state`
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
    let approved = approved_train(Path::new(need("evidence")?))?;
    let (targets, gate) = bind(
        &plan,
        &approved,
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
