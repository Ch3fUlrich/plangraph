//! Production bindings of the replan job (`round_replan`) and the loop that runs it.
//!
//! Each port is one command of a tool the host already has: the spawner route for the legs, `gh`
//! for the PR's files and its close, the keeper CLI for the item text and the slice definitions,
//! the fleet bus client for the routing line. The commands go through `Exec`, so the tests drive
//! the whole path with a recording fake.
use crate::adapter::{run_command, Adapter};
use crate::errors::ToolError;
use crate::placement::parameter;
use crate::round_replan::{self as replan, FileChange, Leg, PlanInput, Ports, Slice};
use pg_worker_core::run::record_io::atomic_write_private;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::Duration;

#[cfg(test)]
#[path = "round_replan_ports_tests.rs"]
mod tests;

/// Pause between two passes over the queued jobs.
const PASS_INTERVAL: Duration = Duration::from_secs(30);
const COMMAND_TIMEOUT: Duration = Duration::from_secs(120);

#[derive(Clone, Copy, Debug, PartialEq)]
pub(crate) enum Tool {
    Spawner,
    Gh,
    Keeper,
    Bus,
}

/// Runs one command of a tool and returns its stdout; a non-zero exit is a refusal that carries
/// the last stderr line.
pub(crate) trait Exec {
    fn run(&self, tool: Tool, args: &[String]) -> Result<String, ToolError>;
}

pub(crate) struct SystemExec<'a>(pub &'a Path);

fn python() -> Command {
    Command::new(
        std::env::var_os("PG_SPAWND_PYTHON")
            .unwrap_or_else(|| if cfg!(windows) { "python" } else { "python3" }.into()),
    )
}

impl Exec for SystemExec<'_> {
    fn run(&self, tool: Tool, args: &[String]) -> Result<String, ToolError> {
        let module = |name: &str| {
            let mut cmd = python();
            cmd.args(["-m", name]).args(args);
            cmd
        };
        let (code, stdout, stderr) = match tool {
            Tool::Spawner => Adapter(self.0).run(args, COMMAND_TIMEOUT)?,
            Tool::Gh => {
                let mut cmd = Command::new("gh");
                cmd.args(args);
                run_command(cmd, COMMAND_TIMEOUT)?
            }
            Tool::Keeper => run_command(module("plangraph.keeper.cli"), COMMAND_TIMEOUT)?,
            Tool::Bus => run_command(module("plangraph.fleet_bus.client"), COMMAND_TIMEOUT)?,
        };
        if code != 0 {
            let name = format!("{tool:?}").to_lowercase();
            return Err(parameter(format!("replan_{name}_failed: {stderr}")));
        }
        Ok(String::from_utf8_lossy(&stdout).into_owned())
    }
}

pub(crate) struct CommandPorts<'a> {
    pub exec: &'a dyn Exec,
    pub state: &'a Path,
}

fn strings(args: &[&str]) -> Vec<String> {
    args.iter().map(|a| a.to_string()).collect()
}
fn parse(text: &str, what: &str) -> Result<Value, ToolError> {
    serde_json::from_str(text).map_err(|_| parameter(format!("replan_{what}_invalid: not JSON")))
}

impl CommandPorts<'_> {
    fn show(&self, key: &str) -> Result<Value, ToolError> {
        parse(
            &self
                .exec
                .run(Tool::Keeper, &strings(&["--json", "show", key]))?,
            "keeper",
        )
    }
}

/// The confirmed findings of the latest validated review round of `item` on `pr`, verbatim,
/// `<SEVERITY> <text>`. The rounds are the admission's own history (voided and refused attempts
/// left out); a round whose verdict is missing or invalid is skipped for the one before it.
fn last_round_findings(state: &Path, item: &str, pr: &str) -> Result<Vec<String>, ToolError> {
    last_round_findings_of(state, item, &json!(pr))
}

/// The same for a round loop that has no PR (`pr` null).
pub(crate) fn last_round_findings_of(
    state: &Path,
    item: &str,
    pr: &Value,
) -> Result<Vec<String>, ToolError> {
    let history = crate::round_progress::round_history(state, item)?;
    for record in history.iter().rev().filter(|r| r["round_pr"] == *pr) {
        let run = record["run_id"].as_str().unwrap_or("");
        let Ok(dir) = pg_worker_core::run::record::run_dir(state, run) else {
            continue;
        };
        let snapshot = dir.join("round-verdict.json");
        let verdict = if snapshot.exists() {
            crate::round_progress::read_json(&snapshot).map(|saved| saved["verdict"].clone())
        } else {
            crate::round_progress::read_json(Path::new(
                record["round_verdict_file"].as_str().unwrap_or(""),
            ))
        };
        let Ok(verdict) = verdict else { continue };
        if crate::round_progress::summarize(&verdict, run, item, pr).is_err() {
            continue;
        }
        return Ok(verdict["findings"]
            .as_array()
            .map(|all| {
                all.iter()
                    .filter(|f| f["confirmed"] == true)
                    .filter_map(|f| {
                        Some(format!(
                            "{} {}",
                            f["severity"].as_str()?,
                            f["text"].as_str()?
                        ))
                    })
                    .collect()
            })
            .unwrap_or_default());
    }
    Ok(vec![])
}

impl Ports for CommandPorts<'_> {
    /// The legs the spawner route serves for the orchestrate role, each with its catalog
    /// intelligence (`--leg-scores`; an unscored leg counts 0); a leg the route skipped is not in
    /// the list.
    fn legs(&self) -> Result<Vec<Leg>, ToolError> {
        let card = json!({"item_key":"replan", "estimate_tokens":20000}).to_string();
        let out = self.exec.run(
            Tool::Spawner,
            &strings(&[
                "route",
                "--role",
                "orchestrate",
                "--json",
                "--leg-scores",
                "--card",
                &card,
            ]),
        )?;
        let route = parse(&out, "route")?;
        let legs = route["legs"].as_array().cloned().unwrap_or_default();
        Ok(legs
            .iter()
            .filter_map(|leg| {
                let name = leg["id"].as_str()?.to_string();
                Some(Leg {
                    intelligence: route["leg_scores"][&name]
                        .as_f64()
                        .filter(|n| n.is_finite())
                        .unwrap_or(0.0)
                        .max(0.0),
                    name,
                    planning: leg["roles"]
                        .as_array()
                        .is_some_and(|r| r.iter().any(|r| r == "orchestrate")),
                    available: true,
                })
            })
            .collect())
    }

    fn pr_files(&self, pr: &str) -> Result<Vec<FileChange>, ToolError> {
        let out = self
            .exec
            .run(Tool::Gh, &strings(&["pr", "view", pr, "--json", "files"]))?;
        let view = parse(&out, "gh")?;
        let files = view["files"]
            .as_array()
            .ok_or_else(|| parameter("replan_gh_invalid: no files in the PR view"))?;
        Ok(files
            .iter()
            .filter_map(|f| {
                Some(FileChange {
                    path: f["path"].as_str()?.to_string(),
                    lines: f["additions"].as_u64().unwrap_or(0)
                        + f["deletions"].as_u64().unwrap_or(0),
                })
            })
            .collect())
    }

    fn item_text(&self, item: &str) -> Result<String, ToolError> {
        Ok(self.show(item)?["description"]
            .as_str()
            .unwrap_or("")
            .to_string())
    }

    fn open_findings(&self, item: &str, pr: &str) -> Result<Vec<String>, ToolError> {
        last_round_findings(self.state, item, pr)
    }

    /// The deterministic grouping stands in for the leg's own: it already honours the line
    /// target and covers every counted file once.
    fn plan(&self, _leg: &Leg, input: &PlanInput) -> Result<Vec<Vec<String>>, ToolError> {
        Ok(input.proposal.to_vec())
    }

    fn define_slice(&self, slice: &Slice) -> Result<(), ToolError> {
        // A slice the keeper already knows is defined: a retry after a crash adds nothing.
        if self.show(&slice.key).is_ok() {
            return Ok(());
        }
        let parent = self.show(&slice.parent)?;
        let (project, id) = slice
            .key
            .split_once(':')
            .ok_or_else(|| parameter("replan_slice_invalid: key has no project"))?;
        let findings = slice.findings.join("\n");
        let item = json!({
            "project":project, "id":id,
            "title":format!("{} slice {}", slice.parent, id.rsplit("-S").next().unwrap_or(id)),
            "kind":parent["kind"].as_str().unwrap_or("lane"),
            "track":parent["track"].as_str().unwrap_or("T0"),
            "owner":parent["owner"].as_str().unwrap_or(""),
            "estimate_h":1, "depends_on":[], "operator":false,
            // The parent depends on its slices, so they reach the goal through it.
            "under":slice.parent,
            "slice_parent":slice.parent,
            "done_criteria":format!("Slice of {}: {} changed lines in {} files; every finding below fixed",
                slice.parent, slice.lines, slice.files.len()),
            "evidence_kinds":["any"],
            "description":format!(
                "Why: the review of {} did not converge within the round rule (rule 3.35), so the PR is replanned into slices.\nHow: implement only these files: {}.\nOpen findings of the replanned PR, verbatim:\n{}",
                slice.parent, slice.files.join(", "), findings),
        });
        // Not in `replans/`: every `.json` there is a job.
        let dir = self.state.join("replan-define");
        std::fs::create_dir_all(&dir)
            .map_err(|_| parameter("replan_state_unavailable: define directory missing"))?;
        let path = dir.join(format!("{:x}.json", Sha256::digest(slice.key.as_bytes())));
        atomic_write_private(&path, json!({"items":[item]}).to_string().as_bytes())
            .map_err(|_| parameter("replan_state_unavailable: define file not written"))?;
        let file = path.to_string_lossy().into_owned();
        self.exec
            .run(Tool::Keeper, &strings(&["define", "--file", &file]))
            .map(|_| ())
    }

    fn close_pr_unmerged(&self, pr: &str) -> Result<(), ToolError> {
        self.exec
            .run(
                Tool::Gh,
                &strings(&[
                    "pr",
                    "close",
                    pr,
                    "--comment",
                    "Closed unmerged: the round rule stopped this writer loop; the work continues as slice items.",
                ]),
            )
            .map(|_| ())
    }

    fn send_routing(&self, key: &str, line: &str) -> Result<(), ToolError> {
        let out = self.exec.run(
            Tool::Bus,
            &strings(&["send", "--msg-id", key, "routing", line]),
        )?;
        // `fleet-msg` exits 0 for a line it only queued locally ("queued <id> (will retry)");
        // the receiver's receipt is "sent <id> seq=<n>" with the id we sent and a numeric
        // sequence. Anything else is not an acknowledgment.
        if is_receipt(&out, key) {
            Ok(())
        } else {
            Err(parameter("replan_bus_unacknowledged: no receiver receipt"))
        }
    }
}

/// `sent <key> seq=<digits>`: the receipt of a line the receiver took under `key`.
fn is_receipt(out: &str, key: &str) -> bool {
    let mut words = out.split_whitespace();
    let (Some("sent"), Some(id), Some(seq), None) =
        (words.next(), words.next(), words.next(), words.next())
    else {
        return false;
    };
    id == key
        && seq
            .strip_prefix("seq=")
            .is_some_and(|n| !n.is_empty() && n.bytes().all(|b| b.is_ascii_digit()))
}

/// One pass over the queued jobs with `exec`.
pub(crate) fn pass(state: &Path, exec: &dyn Exec) -> Vec<(String, String)> {
    replan::run_pending(state, &CommandPorts { exec, state })
}

/// Run the queued replan jobs for as long as the process lives. A failed step stays saved in its
/// job file and the next pass retries it.
pub(crate) fn start(state: PathBuf) {
    std::thread::spawn(move || loop {
        for (job, outcome) in pass(&state, &SystemExec(&state)) {
            if outcome != "done" && outcome != "busy" {
                eprintln!("replan {job}: {outcome}");
            }
        }
        std::thread::sleep(PASS_INTERVAL);
    });
}
