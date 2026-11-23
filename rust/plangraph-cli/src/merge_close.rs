//! `plangraph merge-close`: the keeper closes every item a merge into main names.
//!
//! One keeper call (`close_merged_auto`) reads the default branch and closes each open,
//! claimed or in-review item named by a `Plan-Item` trailer: no router, no decision id.
//! The job prints one line per closed item and one line per key the keeper refused or
//! flagged, with the reason, so routing sees every merged key that stayed open.
//! A timer runs it every ten minutes, and the batch land step calls [`run_once`].
use crate::feeder::{Env, SlowKeeper, Transport};
use serde_json::{json, Value};
use std::fmt::Write as _;

/// The keeper reads git for every candidate, so the call gets far more than a read verb.
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
    let mut body = json!({"apply": apply});
    if let Some(require_seats) = require_seats {
        body["require_seats"] = json!(require_seats);
    }
    if let Some(project) = project {
        body["project"] = json!(project);
    }
    let result = keeper.call("close_merged_auto", Some(body))?;
    let rows = |name: &str| result[name].as_array().cloned().unwrap_or_default();
    let merged_not_done = result["merged_not_done"]
        .as_u64()
        .ok_or("keeper reply has no merged_not_done count")?;
    let mut text = String::new();
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
    let _ = writeln!(text, "merged-not-done\t{merged_not_done}");
    Ok(Outcome {
        text,
        merged_not_done,
    })
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

/// The call the land step makes after a merge: the job with the configured switch.
pub fn run_once(env: &Env, project: Option<&str>) -> Result<Outcome, String> {
    let keeper = SlowKeeper {
        env,
        max_time_secs: MAX_TIME_SECS,
    };
    run_with(&keeper, require_seats(env)?, true, project)
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
    let outcome = if count {
        let keeper = SlowKeeper {
            env,
            max_time_secs: MAX_TIME_SECS,
        };
        require_seats(env)
            .and_then(|seats| run_with(&keeper, seats, false, project))
            .map(|o| Outcome {
                text: format!("{}\n", o.merged_not_done),
                ..o
            })
    } else {
        run_once(env, project)
    };
    match outcome {
        Ok(done) => {
            print!("{}", done.text);
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

/// Persist one routing proposal batch from the keeper's watched dry run.
pub fn reconcile_with(
    keeper: &dyn Transport,
    seats: Option<bool>,
    state: &std::path::Path,
) -> Result<String, String> {
    let mut request = json!({"apply": false, "watch": true});
    if let Some(required) = seats {
        request["require_seats"] = json!(required);
    }
    let report = keeper.call("close_merged_auto", Some(request))?;
    let count = report["merged_not_done"]
        .as_u64()
        .ok_or("missing merged count")?;
    let alert = report["alert"]
        .as_bool()
        .ok_or("missing watcher alert state")?;
    let close = report["would_close"]
        .as_array()
        .ok_or("missing close proposals")?;
    let listed = report["listed"]
        .as_array()
        .ok_or("missing refused proposals")?;
    if count != (close.len() + listed.len()) as u64 {
        return Err("merged count does not match the proposal rows".into());
    }
    let mut proposals = Vec::new();
    let mut refusals = std::collections::BTreeMap::new();
    for row in close {
        proposals.push(json!({"action": "CLOSE", "item": row}));
    }
    for row in listed {
        let reason = row["reason"].as_str().ok_or("missing refusal reason")?;
        let (code, detail) = reason.split_once(": ").unwrap_or((reason, ""));
        let tag = detail.split(':').next().unwrap_or_default();
        let tag = if !tag.is_empty() && tag.bytes().all(|b| b.is_ascii_lowercase() || b == b'_') {
            tag
        } else {
            code
        };
        *refusals.entry(tag).or_insert(0usize) += 1;
        let action = if tag == "unmerged_work_or_lanes_unknown" {
            "SLICE-OPEN"
        } else {
            "RESCOPE"
        };
        proposals.push(json!({"action": action, "item": row}));
    }
    let batch = json!({"merged_not_done": count, "alert": alert, "refusals": refusals,
                       "proposals": proposals});
    let text = batch.to_string();
    pg_worker_core::run::record_io::atomic_write_private(state, text.as_bytes())
        .map_err(|err| format!("write watcher state {}: {err}", state.display()))?;
    Ok(format!("reconcile-merged\t{text}"))
}

pub fn reconcile(env: &Env, args: &[String]) -> i32 {
    let [dry_run, flag, state] = args else {
        eprintln!("usage: plangraph reconcile-merged --dry-run --state PATH");
        return 2;
    };
    if dry_run != "--dry-run" || flag != "--state" {
        eprintln!("usage: plangraph reconcile-merged --dry-run --state PATH");
        return 2;
    }
    let keeper = SlowKeeper {
        env,
        max_time_secs: MAX_TIME_SECS,
    };
    match require_seats(env).and_then(|seats| reconcile_with(&keeper, seats, state.as_ref())) {
        Ok(line) => {
            println!("{line}");
            0
        }
        Err(error) => {
            eprintln!("reconcile-merged: {error}");
            1
        }
    }
}
