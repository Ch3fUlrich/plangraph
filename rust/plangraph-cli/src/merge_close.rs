//! `plangraph merge-close`: the keeper closes every item a merge into main names.
//!
//! The timer refreshes the merge index, then closes each open,
//! claimed or in-review item named by a `Plan-Item` trailer: no router, no decision id.
//! The job prints one line per closed item and one line per key the keeper refused or
//! flagged, with the reason, so routing sees every merged key that stayed open.
//! A timer runs it every ten minutes, and the batch land step calls [`run_once`].
use crate::feeder::{Env, SlowKeeper, Transport};
use serde_json::{json, Value};
use std::collections::BTreeMap;
use std::fmt::Write as _;
use std::time::{Duration, Instant};

/// Keep a bound on each keeper call, including a page of close results.
const MAX_TIME_SECS: u32 = 300;
const SWITCH: &str = "PG_MERGE_CLOSE_REQUIRE_SEATS";

/// What one run did, as the keeper reported it.
#[derive(Debug, PartialEq)]
pub struct Outcome {
    pub text: String,
    pub merged_not_done: u64,
}

/// The `require_seats` switch: unset leaves the keeper's own `require_seats` setting in force
/// (the request omits the field); `1`, `true` or `on` demands seats, `0`, `false` or `off` flags a
/// missing seat instead.
pub fn require_seats(env: &Env) -> Result<Option<bool>, String> {
    match env
        .get(SWITCH)
        .map(|v| v.trim().to_ascii_lowercase())
        .as_deref()
    {
        None | Some("") => Ok(None),
        Some("1" | "true" | "on") => Ok(Some(true)),
        Some("0" | "false" | "off") => Ok(Some(false)),
        Some(_) => Err(format!("{SWITCH} must be 1/true/on or 0/false/off")),
    }
}

/// Close what is merged (`apply`) or only count it, through any keeper transport.
pub fn run_with(
    keeper: &dyn Transport,
    require_seats: Option<bool>,
    apply: bool,
    project: Option<&str>,
) -> Result<Outcome, String> {
    let mut pass = ClosePass::default();
    pass.collect(keeper, require_seats, apply, project)?;
    Ok(pass.finish())
}

#[derive(Default)]
struct ClosePass {
    text: String,
    merged_not_done: u64,
    scan_errors: BTreeMap<String, String>,
}

impl ClosePass {
    fn collect(
        &mut self,
        keeper: &dyn Transport,
        require_seats: Option<bool>,
        apply: bool,
        project: Option<&str>,
    ) -> Result<(), String> {
        let mut body = json!({"apply": apply});
        if let Some(require_seats) = require_seats {
            body["require_seats"] = json!(require_seats);
        }
        if let Some(project) = project {
            body["project"] = json!(project);
        }
        loop {
            let result = keeper.call("close_merged_auto", Some(body.clone()))?;
            self.merged_not_done += result["merged_not_done"]
                .as_u64()
                .ok_or("keeper reply has no merged_not_done count")?;
            if let Some(errors) = result["scan_errors"].as_object() {
                for (repo, reason) in errors {
                    let reason = reason.as_str().ok_or("keeper scan error is not text")?;
                    self.scan_errors
                        .insert(repo.clone(), reason.replace(['\n', '\t'], " "));
                }
            }
            page_text(&mut self.text, &result);
            let Some(after) = result["next_after"].as_str() else {
                return if result["next_after"].is_null() {
                    Ok(())
                } else {
                    Err("keeper continuation is not an item key".into())
                };
            };
            if after.is_empty() || body["after"].as_str().is_some_and(|prior| after <= prior) {
                return Err("keeper continuation did not advance".into());
            }
            body["after"] = json!(after);
        }
    }

    fn finish(mut self) -> Outcome {
        for (repo, reason) in self.scan_errors {
            let _ = writeln!(self.text, "scan-error\t{repo}\t{reason}");
        }
        let merged_not_done = self.merged_not_done;
        let _ = writeln!(self.text, "merged-not-done\t{merged_not_done}");
        Outcome {
            text: self.text,
            merged_not_done,
        }
    }
}

fn page_text(text: &mut String, result: &Value) {
    let rows = |name: &str| result[name].as_array().into_iter().flatten();
    for (label, name) in [("closed", "closed"), ("would-close", "would_close")] {
        for row in rows(name) {
            let heads: Vec<&str> = row["prs"]
                .as_array()
                .into_iter()
                .flatten()
                .filter_map(|p| p["head"].as_str())
                .collect();
            let _ = writeln!(
                text,
                "{label}\t{}\t{}{}",
                row["key"].as_str().unwrap_or("?"),
                heads.join(","),
                flagged(&row["flags"])
            );
        }
    }
    for row in rows("parts") {
        let _ = writeln!(
            text,
            "merged-part\t{}\t{}\t{}",
            row["key"].as_str().unwrap_or("?"),
            row["pr"],
            row["merge_sha"].as_str().unwrap_or("?")
        );
    }
    for row in rows("listed") {
        let _ = writeln!(
            text,
            "listed\t{}\t{}\t{}",
            row["key"].as_str().unwrap_or("?"),
            row["state"].as_str().unwrap_or("?"),
            row["reason"]
                .as_str()
                .unwrap_or("?")
                .replace(['\n', '\t'], " ")
        );
    }
}

/// `\tFLAGGED name: run ids; ...` for the flags a close stored (`seats_missing`), else nothing.
fn flagged(flags: &Value) -> String {
    let Some(flags) = flags.as_object().filter(|f| !f.is_empty()) else {
        return String::new();
    };
    let parts: Vec<String> = flags
        .iter()
        .map(|(name, flag)| {
            let runs: Vec<&str> = flag["run_ids"]
                .as_array()
                .into_iter()
                .flatten()
                .filter_map(Value::as_str)
                .collect();
            if runs.is_empty() {
                name.clone()
            } else {
                format!("{name}: {}", runs.join(","))
            }
        })
        .collect();
    format!("\tFLAGGED {}", parts.join("; "))
}

/// The repositories the keeper's merged-on-default scan reads (`repos`, or `merge_scan` of its config).
pub fn repos_with(keeper: &dyn Transport) -> Result<Vec<String>, String> {
    let result = keeper.call("close_merged_auto", Some(json!({"repos_only": true})))?;
    result["repos"]
        .as_array()
        .ok_or("keeper reply has no repos list")?
        .iter()
        .map(|name| {
            name.as_str()
                .map(str::to_owned)
                .ok_or("keeper repo name is not text".to_owned())
        })
        .collect()
}

fn run_scheduled(env: &Env, apply: bool, project: Option<&str>) -> Result<Outcome, String> {
    let seats = require_seats(env)?;
    let keeper = SlowKeeper {
        env,
        max_time_secs: MAX_TIME_SECS,
    };
    let control = SlowKeeper {
        env,
        max_time_secs: 10,
    };
    control.call("close_merged_auto", Some(json!({"refresh": "start"})))?;
    let deadline = Instant::now() + Duration::from_secs(95);
    loop {
        let reply = control.call("close_merged_auto", Some(json!({"refresh": "status"})))?;
        match reply["scan"].as_str() {
            Some("ready") => break,
            Some("running") if Instant::now() < deadline => {
                std::thread::sleep(Duration::from_secs(1))
            }
            Some("running") => return Err("merge index refresh is still running".into()),
            _ => return Err("keeper reply has no valid merge index status".into()),
        }
    }
    let mut pass = ClosePass::default();
    if let Some(project) = project {
        pass.collect(&keeper, seats, apply, Some(project))?;
    } else {
        let reply = control.call("close_merged_auto", Some(json!({"refresh": "projects"})))?;
        for project in reply["projects"]
            .as_array()
            .ok_or("keeper reply has no project list")?
        {
            let project = project.as_str().ok_or("keeper project name is not text")?;
            pass.collect(&keeper, seats, apply, Some(project))?;
        }
    }
    if !apply && !pass.scan_errors.is_empty() {
        return Err(format!("incomplete merge scan: {:?}", pass.scan_errors));
    }
    Ok(pass.finish())
}

/// The call the land step makes after a merge: the job with the configured switch.
pub fn run_once(env: &Env, project: Option<&str>) -> Result<Outcome, String> {
    run_scheduled(env, true, project)
}

/// `merge-close [--count | --repos] [--project NAME]`; `--count` prints only the number of
/// merged items that are not done and `--repos` the scanned repositories, one per line;
/// neither changes anything.
pub fn run(env: &Env, args: &[String]) -> i32 {
    let mut count = false;
    let mut repos = false;
    let mut project = None;
    let mut rest = args.iter();
    while let Some(arg) = rest.next() {
        match arg.as_str() {
            "--count" => count = true,
            "--repos" => repos = true,
            "--project" => match rest.next() {
                Some(name) => project = Some(name.as_str()),
                None => return usage(),
            },
            _ => return usage(),
        }
    }
    if repos {
        let keeper = SlowKeeper {
            env,
            max_time_secs: MAX_TIME_SECS,
        };
        return match repos_with(&keeper) {
            Ok(names) => {
                names.iter().for_each(|name| println!("{name}"));
                0
            }
            Err(reason) => {
                eprintln!("merge-close: {reason}");
                1
            }
        };
    }
    let outcome = run_scheduled(env, !count, project);
    match outcome {
        Ok(done) => {
            if count {
                println!("{}", done.merged_not_done);
            } else {
                print!("{}", done.text);
            }
            0
        }
        Err(reason) => {
            eprintln!("merge-close: {reason}");
            1
        }
    }
}

fn usage() -> i32 {
    eprintln!("usage: plangraph merge-close [--count | --repos] [--project NAME]");
    2
}
