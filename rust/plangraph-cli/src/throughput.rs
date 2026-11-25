//! Hourly throughput collection in the landing database, with UTC daily rollups.
mod database;
mod runs;

use chrono::{DateTime, Utc};
use serde_json::{json, Value};
use std::{
    collections::BTreeMap,
    fs,
    path::{Path, PathBuf},
    process::Command,
};

const HOUR: i64 = 3600;
const BASELINE_START: &str = "2026-11-20T00:00:00Z";
type Env = BTreeMap<String, String>;

#[derive(Default)]
struct Hour {
    commits: u64,
    items_closed: u64,
}

struct Arrival {
    sha: String,
    at: i64,
    commits: u64,
    /// Every commit the arrival made reachable, oldest first, with its parents.
    members: Vec<(String, Vec<String>)>,
    items: Vec<String>,
    /// How many parts the lander recorded for the landing this arrival stands for; `None` for a plain arrival.
    parts: Option<usize>,
    /// The part heads the landing's history shows; `None` for a plain arrival.
    part_heads: Option<Vec<String>>,
}

fn git(args: &[&str]) -> Result<String, String> {
    let mut command = Command::new("git");
    let output = pg_worker_core::host_command::output(
        command.args(args),
        std::time::Duration::from_secs(30),
    )
    .map_err(|e| format!("cannot read main history: {e}"))?;
    String::from_utf8(output).map_err(|_| "non-UTF8 Git response".into())
}

fn arrivals(reference: &str, project: &str, start: i64, end: i64) -> Result<Vec<Arrival>, String> {
    let revision = git(&[
        "rev-parse",
        "--verify",
        "--end-of-options",
        &format!("{reference}^{{commit}}"),
    ])?;
    let lines = git(&[
        "log",
        "--first-parent",
        "--reverse",
        "--format=%H %ct %P",
        &format!("--since-as-filter=@{start}"),
        &format!("--until=@{}", end - 1),
        revision.trim(),
    ])?;
    lines
        .lines()
        .map(|line| {
            let mut fields = line.split_whitespace();
            let sha = fields.next().ok_or("landing SHA missing")?;
            let at = fields
                .next()
                .and_then(|value| value.parse::<i64>().ok())
                .ok_or("invalid Git time")?;
            let parents: Vec<String> = fields.map(str::to_owned).collect();
            let range = parents
                .first()
                .map_or_else(|| sha.to_owned(), |parent| format!("{parent}..{sha}"));
            let members: Vec<(String, Vec<String>)> =
                git(&["rev-list", "--parents", "--reverse", &range])?
                    .lines()
                    .map(|line| {
                        let mut fields = line.split_whitespace().map(str::to_owned);
                        (fields.next().unwrap_or_default(), fields.collect())
                    })
                    .collect();
            let trailers = git(&[
                "log",
                "--format=%(trailers:key=Plan-Item,valueonly)",
                &range,
            ])?;
            let items = trailers
                .lines()
                .map(str::trim)
                .filter_map(|value| {
                    let (owner, key) = value.split_once(':')?;
                    (owner == project
                        && !key.is_empty()
                        && key
                            .bytes()
                            .all(|byte| byte.is_ascii_alphanumeric() || b"._:-".contains(&byte)))
                    .then(|| value.to_owned())
                })
                .collect();
            Ok(Arrival {
                sha: sha.into(),
                at,
                commits: members.len() as u64,
                members,
                items,
                parts: None,
                part_heads: None,
            })
        })
        .collect()
}

/// What each landing in the window brought to main, at the landing's own time, and which part heads it held. The
/// lander builds a train by merging each part's head, so the parts are the second parents of the merges on the
/// first-parent chain from the train's base to its tip: for a merging landing the chain is `first-parent..second-
/// parent` of the landed merge, for a tested fast-forward `latest recorded landing in the ancestry..landed` (never a
/// landing chosen by clock). A head that reached main on its own is no such second parent, so it never counts. The
/// landed SHA must lie on the selected main ref, and the number of parts found must equal the number the lander
/// recorded, else the join stays unverified. A landing with no verified base joins nothing here.
fn train_arrivals(
    db: &database::Db,
    repo: &str,
    reference: &str,
    start: i64,
    end: i64,
) -> Result<Vec<Arrival>, String> {
    let ancestor =
        |older: &str, newer: &str| git(&["merge-base", "--is-ancestor", older, newer]).is_ok();
    let main = git(&[
        "rev-parse",
        "--verify",
        "--end-of-options",
        &format!("{reference}^{{commit}}"),
    ])?;
    let main = main.trim();
    let landings: Vec<database::Landing> = db
        .landings(repo)?
        .into_iter()
        .filter(|landing| ancestor(&landing.sha, main))
        .collect();
    let mut trains = Vec::new();
    for landing in &landings {
        let (sha, at, parts) = (&landing.sha, &landing.at, &landing.parts);
        if !(start..end).contains(at) {
            continue;
        }
        // A merge commit names its own base: the first parent is main as the train met it, so what the merge
        // brought is exactly `first-parent..landed`, whatever else landed before. A fast-forward has no such
        // record: its base is the latest recorded landing in its ancestry (the one no other ancestor landing
        // descends from), and none means no verified base.
        // The lander's mode says which: a tested fast-forward lands the tip of a chain that may itself end in a
        // merge, so only the merging modes name their base in the first parent.
        let merging = matches!(
            landing.mode.as_deref(),
            Some("train-pr-merge" | "single-pr")
        );
        let merge =
            merging && git(&["rev-parse", "--verify", "--quiet", &format!("{sha}^2")]).is_ok();
        let (base, tip) = if merge {
            (format!("{sha}^1"), format!("{sha}^2"))
        } else {
            let earlier: Vec<&String> = landings
                .iter()
                .map(|other| &other.sha)
                .filter(|other| *other != sha && ancestor(other, sha))
                .collect();
            let Some(base) = earlier.iter().find(|candidate| {
                earlier
                    .iter()
                    .all(|other| other == *candidate || !ancestor(candidate, other))
            }) else {
                continue;
            };
            ((*base).clone(), sha.clone())
        };
        let range = format!("{base}..{sha}");
        let Ok(listing) = git(&["rev-list", "--parents", "--reverse", "-n", "20000", &range])
        else {
            continue;
        };
        let members: Vec<(String, Vec<String>)> = listing
            .lines()
            .map(|line| {
                let mut fields = line.split_whitespace().map(str::to_owned);
                (fields.next().unwrap_or_default(), fields.collect())
            })
            .collect();
        let merges = git(&[
            "log",
            "--first-parent",
            "--merges",
            "--format=%P",
            &format!("{base}..{tip}"),
        ])
        .unwrap_or_default();
        let mut part_heads: Vec<String> = merges
            .lines()
            .flat_map(|parents| parents.split_whitespace().skip(1).map(str::to_owned))
            .collect();
        if part_heads.is_empty() {
            // A train of one part with no merge: its tip is the part's head.
            part_heads.extend(
                git(&["rev-parse", "--verify", &format!("{tip}^{{commit}}")])
                    .map(|s| s.trim().to_owned()),
            );
        }
        trains.push(Arrival {
            sha: sha.clone(),
            at: *at,
            commits: members.len() as u64,
            members,
            items: Vec::new(),
            parts: *parts,
            part_heads: Some(part_heads),
        });
    }
    Ok(trains)
}

fn timestamp(text: &str) -> Result<i64, String> {
    DateTime::parse_from_rfc3339(text)
        .map(|date| date.timestamp())
        .map_err(|_| "expected an RFC3339 timestamp".into())
}

fn snapshot(
    path: &Path,
    project: &str,
    start: i64,
    end: i64,
    hours: &mut BTreeMap<i64, Hour>,
) -> Result<(), String> {
    let data: Value = read_input(path)?;
    if let Some(generated) = data["generated"].as_str() {
        if timestamp(generated)? < end {
            return Err("keeper snapshot predates the requested cutoff".into());
        }
    }
    let items = data["items"]
        .as_array()
        .ok_or("keeper snapshot has no items array")?;
    let mut seen = std::collections::HashSet::new();
    for item in items
        .iter()
        .filter(|v| v["project"] == project && v["state"] == "done")
    {
        let Some(done) = item["done_at"].as_str() else {
            continue;
        };
        let at = timestamp(done)?;
        if (start..end).contains(&at) {
            let key = item["key"].as_str().ok_or("keeper item has no key")?;
            if seen.insert(key) {
                hours
                    .get_mut(&(at.div_euclid(HOUR) * HOUR))
                    .unwrap()
                    .items_closed += 1;
            }
        }
    }
    Ok(())
}

fn read_input(path: &Path) -> Result<Value, String> {
    let data = fs::read(path).map_err(|e| format!("cannot read throughput input: {e}"))?;
    serde_json::from_slice(&data).map_err(|e| format!("invalid throughput input: {e}"))
}

fn collect(
    db: &database::Db,
    env: &Env,
    repo: &str,
    project: &str,
    reference: &str,
    keeper: &Path,
    window: std::ops::Range<i64>,
) -> Result<Value, String> {
    let (start, end) = (window.start, window.end);
    if start < 0 || start % HOUR != 0 || end <= start || end - start > 31 * 24 * HOUR {
        return Err(
            "collection requires an hour-aligned start and a range of at most 31 days".into(),
        );
    }
    db.check_landing()?;
    let mut hours: BTreeMap<_, _> = (start..end)
        .step_by(HOUR as usize)
        .map(|t| (t, Hour::default()))
        .collect();
    let arrivals = arrivals(reference, project, start, end)?;
    let trains = train_arrivals(db, repo, reference, start, end)?;
    for arrival in &arrivals {
        hours
            .get_mut(&(arrival.at.div_euclid(HOUR) * HOUR))
            .ok_or("landing outside collection window")?
            .commits += arrival.commits;
    }
    snapshot(keeper, project, start, end, &mut hours)?;
    let inputs = runs::Inputs::load(env)?;
    db.store(repo, end, &hours, &arrivals, &trains, &inputs)?;
    let display =
        |seconds| DateTime::<Utc>::from_timestamp(seconds, 0).map(|time| time.to_rfc3339());
    Ok(json!({"hours": hours.len(), "from": display(start), "through": display(end)}))
}

fn database_path(home: &Path, env: &Env) -> PathBuf {
    if let Some(path) = env.get("PLANGRAPH_LANDING_DB").filter(|p| !p.is_empty()) {
        return path.into();
    }
    env.get("XDG_DATA_HOME")
        .filter(|p| !p.is_empty())
        .map(PathBuf::from)
        .unwrap_or_else(|| home.join(".local/share"))
        .join("plangraph/landing-telemetry.sqlite3")
}

fn execute(home: &Path, env: &Env, args: &[String]) -> Result<Value, String> {
    let db = database::Db(database_path(home, env));
    match args {
        [verb, repo, project, reference, keeper, start, end] if verb == "collect" => collect(
            &db, env, repo, project, reference, Path::new(keeper), timestamp(start)?..timestamp(end)?,
        ),
        [verb, repo] if verb == "report" => db.report(repo),
        [verb, repo, project, reference, keeper] if verb == "tick" => {
            let end = Utc::now().timestamp().div_euclid(HOUR) * HOUR;
            db.check_landing()?;
            let configured = timestamp(env.get("PLANGRAPH_THROUGHPUT_START")
                .map_or(BASELINE_START, String::as_str))?;
            let start = db.through(repo)?.map(|through| {
                (through.div_euclid(HOUR) * HOUR).min(end - 2 * HOUR)
            }).unwrap_or(configured);
            let end = end.min(start.saturating_add(31 * 24 * HOUR));
            collect(&db, env, repo, project, reference, Path::new(keeper), start..end)
        }
        _ => Err("usage: plangraph throughput collect FORGE_REPO KEEPER_PROJECT MAIN_REF KEEPER_SNAPSHOT FROM_UTC UNTIL_UTC | throughput tick FORGE_REPO KEEPER_PROJECT MAIN_REF KEEPER_SNAPSHOT | throughput report FORGE_REPO".into()),
    }
}

pub fn run(home: &Path, env: &Env, args: &[String]) -> i32 {
    match execute(home, env, args) {
        Ok(result) => {
            println!("{result}");
            0
        }
        Err(reason) => {
            eprintln!("throughput: {reason}");
            2
        }
    }
}
