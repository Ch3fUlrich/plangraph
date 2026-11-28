//! `land-train deploy-apply --plan FILE --landed-head SHA --state FILE [--mode dry-run|apply] [--hosts FILE]
//! [--row-cmd TEMPLATE] [--repo DIR] [--repo-id ID]`: installs the targets of a saved deploy plan on their
//! hosts through `land-train deploy-host`, verifies each first run and writes the keeper deploy rows
//! ([`apply`]).
//!
//! The dry run (the default) changes nothing: it checks the state belongs to this plan and prints each target
//! as `would install` or `already done`. A rerun of the apply skips the finished targets the state records,
//! so running it twice installs nothing twice. `--row-cmd` is the command that records one deploy row, with
//! `{key}` and `{ref}` (the row as JSON) as whole arguments, e.g. `<keeper cli> evidence {key} --kind deploy
//! --ref {ref}`.
use super::deploy::DeployPlan;
use super::deploy_post::{apply, plan_id, Gate, Installer, PostState, RowSink, RunRead, RunTarget};
use super::deploy_probe::{parse_findings, Finding, Probe};
use pg_worker_core::hosts::HostRegistry;
use std::collections::BTreeMap;
use std::path::Path;
use std::process::Command;

/// Runs one `deploy-host` step on a host of the registry: the local host directly, another over SSH.
pub struct HostCmd {
    pub registry: HostRegistry,
    pub repo_id: String,
    pub rev: String,
}

impl HostCmd {
    pub fn argv(&self, host: &str, op: &str, unit: &str) -> Result<Vec<String>, String> {
        let entry = self
            .registry
            .hosts
            .iter()
            .find(|h| h.id == host)
            .ok_or_else(|| format!("host {host} is not in the host registry"))?;
        let root = &entry
            .repos
            .get(&self.repo_id)
            .ok_or_else(|| format!("host {host} has no {} checkout", self.repo_id))?
            .root;
        let step: Vec<String> = [
            "land-train",
            "deploy-host",
            "--op",
            op,
            "--unit",
            unit,
            "--repo",
            root,
            "--rev",
            &self.rev,
        ]
        .iter()
        .map(|s| s.to_string())
        .collect();
        if self.registry.self_host == host {
            let exe = std::env::current_exe().map_err(|e| format!("current executable: {e}"))?;
            return Ok([exe.to_string_lossy().into_owned()]
                .into_iter()
                .chain(step)
                .collect());
        }
        let route = entry
            .ssh_route()
            .ok_or_else(|| format!("host {host} has no SSH route"))?;
        let mut argv: Vec<String> = [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=15",
            "--",
            &route,
        ]
        .iter()
        .map(|s| s.to_string())
        .collect();
        match &entry.wsl_distro {
            Some(distro) => argv.extend([
                "wsl".into(),
                "-d".into(),
                distro.clone(),
                "--".into(),
                "bash".into(),
                "-lc".into(),
                format!("plangraph {}", step.join(" ")),
            ]),
            None => argv.extend(["plangraph".to_owned()].into_iter().chain(step)),
        }
        Ok(argv)
    }

    fn call(&self, host: &str, op: &str, unit: &str) -> Result<(bool, String), String> {
        let argv = self.argv(host, op, unit)?;
        let out = Command::new(&argv[0])
            .args(&argv[1..])
            .output()
            .map_err(|e| format!("{}: {e}", argv[0]))?;
        let stdout = String::from_utf8_lossy(&out.stdout).into_owned();
        if !out.status.success() && op != "check" {
            return Err(format!(
                "{op} {unit} on {host}: {}",
                String::from_utf8_lossy(&out.stderr).trim()
            ));
        }
        Ok((out.status.success(), stdout))
    }
}

impl Installer for HostCmd {
    fn install(&self, host: &str, family: &str) -> Result<Option<String>, String> {
        let (_, out) = self.call(host, "install", family)?;
        Ok(Some(out.trim().to_owned()).filter(|s| !s.is_empty()))
    }
    fn read_run(&self, host: &str, family: &str) -> Result<RunRead, String> {
        let (_, out) = self.call(host, "read", family)?;
        serde_json::from_str(out.trim()).map_err(|e| format!("read {family} on {host}: {e}"))
    }
    fn disable(&self, host: &str, unit: &str) -> Result<(), String> {
        self.call(host, "disable", unit).map(|_| ())
    }
}

impl Probe for HostCmd {
    fn probe(&self, host: &str, family: &str) -> Result<(), Vec<Finding>> {
        let refuse = |why: String| {
            vec![Finding {
                kind: "dry-run".into(),
                name: why,
            }]
        };
        let (ok, out) = self.call(host, "check", family).map_err(refuse)?;
        match parse_findings(&out) {
            Ok(found) if found.is_empty() && ok => Ok(()),
            Ok(found) if !found.is_empty() => Err(found),
            Ok(_) => Err(refuse(format!("dry run of {family} on {host} failed"))),
            Err(e) => Err(refuse(e)),
        }
    }
}

/// Records deploy rows by running a command per row.
pub struct CommandRows {
    pub template: Vec<String>,
}

impl CommandRows {
    pub fn argv(&self, key: &str, row: &str) -> Vec<String> {
        self.template
            .iter()
            .map(|a| match a.as_str() {
                "{key}" => key.to_owned(),
                "{ref}" => row.to_owned(),
                _ => a.clone(),
            })
            .collect()
    }

    /// The keeper evidence ref of a row: every field but the item key, which the command names itself.
    pub fn evidence_ref(row: &super::deploy_post::DeployRow) -> Result<String, String> {
        let mut value = serde_json::to_value(row).map_err(|e| e.to_string())?;
        if let Some(fields) = value.as_object_mut() {
            fields.remove("key");
        }
        Ok(value.to_string())
    }
}

impl RowSink for CommandRows {
    fn write(&self, row: &super::deploy_post::DeployRow) -> Result<(), String> {
        let argv = self.argv(&row.key, &Self::evidence_ref(row)?);
        let (cmd, args) = argv.split_first().ok_or("empty --row-cmd")?;
        let out = Command::new(cmd)
            .args(args)
            .output()
            .map_err(|e| format!("{cmd}: {e}"))?;
        out.status.success().then_some(()).ok_or_else(|| {
            format!(
                "row command for {}: {}",
                row.key,
                String::from_utf8_lossy(&out.stderr).trim()
            )
        })
    }
}

/// What an apply would do, without touching a host or the state. Refuses a state of another plan.
pub fn dry_run(
    targets: &[RunTarget],
    revision: &str,
    state: &PostState,
) -> Result<Vec<String>, String> {
    let pid = plan_id(targets, revision);
    if !state.plan.is_empty() && state.plan != pid {
        return Err(format!(
            "the state belongs to plan {}, this plan is {pid}; clear it for a new plan",
            state.plan
        ));
    }
    let mut lines = vec![format!(
        "dry run of plan {pid} at {revision}; --mode apply installs"
    )];
    if targets.is_empty() {
        lines.push("deploy: none".into());
    }
    lines.extend(
        state
            .failed
            .iter()
            .map(|f| format!("failed earlier {f}: clear it before a rerun")),
    );
    lines.extend(
        state
            .cleanup
            .iter()
            .map(|(h, u)| format!("pending cleanup {u}@{h}")),
    );
    for t in targets {
        let id = format!("{}@{}", t.family, t.host);
        let items: Vec<&str> = t.items.iter().map(String::as_str).collect();
        lines.push(match state.done.contains(&id) {
            true => format!("already done {id}"),
            false => format!(
                "would install {id} rows {}",
                if items.is_empty() {
                    "-".into()
                } else {
                    items.join(",")
                }
            ),
        });
    }
    Ok(lines)
}

fn now_micros() -> i64 {
    chrono::Utc::now().timestamp_micros()
}

pub(super) fn run_cli(flags: &BTreeMap<String, String>) -> Result<Vec<String>, String> {
    let need = |k: &str| {
        flags
            .get(k)
            .cloned()
            .ok_or_else(|| format!("deploy-apply: missing --{k}"))
    };
    let read = |p: &str| std::fs::read(p).map_err(|e| format!("{p}: {e}"));
    let plan: DeployPlan =
        serde_json::from_slice(&read(&need("plan")?)?).map_err(|e| e.to_string())?;
    let targets = plan.run_targets();
    let rev = need("landed-head")?;
    let state_path = need("state")?;
    let mut state: PostState = match std::fs::read(&state_path) {
        Ok(bytes) => serde_json::from_slice(&bytes).map_err(|e| format!("{state_path}: {e}"))?,
        Err(_) => PostState::default(),
    };
    match flags.get("mode").map_or("dry-run", String::as_str) {
        "dry-run" => return dry_run(&targets, &rev, &state),
        "apply" => {}
        m => return Err(format!("deploy-apply: unknown --mode {m} (dry-run, apply)")),
    }
    let repo = flags.get("repo").cloned().unwrap_or_else(|| ".".into());
    let committed = super::git::out(Path::new(&repo), &["show", "-s", "--format=%ct", &rev])?;
    let fresh_after = committed
        .parse::<i64>()
        .map_err(|e| format!("commit time of {rev}: {e}"))?
        * 1_000_000;
    let gate = Gate {
        revision: rev.clone(),
        fresh_after,
        clock: now_micros,
    };
    let registry = HostRegistry::load(Path::new(&need("hosts")?))
        .map_err(|e| format!("host registry: {e}"))?;
    let hosts = HostCmd {
        registry,
        repo_id: flags
            .get("repo-id")
            .cloned()
            .unwrap_or_else(|| "plangraph".into()),
        rev,
    };
    let rows = CommandRows {
        template: need("row-cmd")?
            .split_whitespace()
            .map(str::to_owned)
            .collect(),
    };
    let mut save = |s: &PostState| {
        let bytes = serde_json::to_vec_pretty(s).map_err(|e| e.to_string())?;
        crate::atomic::write(Path::new(&state_path), &bytes)
            .map_err(|e| format!("{state_path}: {e}"))
    };
    let report = apply(&targets, &gate, &hosts, &rows, &mut state, &mut save)?;
    let lines = report.lines();
    match report.failed {
        Some(_) => Err(lines.join("\n")),
        None => Ok(lines),
    }
}
