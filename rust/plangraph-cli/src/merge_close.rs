//! `plangraph merge-close`: the keeper closes every item a merge into main names.
//!
//! One keeper call (`close_merged_auto`) reads the default branch and closes each open,
//! claimed or in-review item named by a `Plan-Item` trailer: no router, no decision id.
//! The job prints one line per closed item and one line per key the keeper refused or
//! flagged, with the reason, so routing sees every merged key that stayed open.
//! A timer runs it every ten minutes, and the batch land step calls [`run_once`].
use crate::feeder::{Env, SlowKeeper, Transport};
use serde_json::json;
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

/// The `require_seats` switch: on unless the environment says `0`, `false` or `off`.
pub fn require_seats(env: &Env) -> Result<bool, String> {
    match env
        .get(SWITCH)
        .map(|v| v.trim().to_ascii_lowercase())
        .as_deref()
    {
        None | Some("" | "1" | "true" | "on") => Ok(true),
        Some("0" | "false" | "off") => Ok(false),
        Some(_) => Err(format!("{SWITCH} must be 1/true/on or 0/false/off")),
    }
}

/// Close what is merged (`apply`) or only count it, through any keeper transport.
pub fn run_with(
    keeper: &dyn Transport,
    require_seats: bool,
    apply: bool,
    project: Option<&str>,
) -> Result<Outcome, String> {
    let mut body = json!({"require_seats": require_seats, "apply": apply});
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
                "{label}\t{}\t{}",
                row["key"].as_str().unwrap_or("?"),
                heads.join(",")
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
