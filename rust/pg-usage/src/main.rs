//! Read-only census. Transcript contents and machine identities never enter output.
use chrono::{DateTime, Duration, NaiveDate, Utc};
use serde_json::{json, Value};
use std::{
    collections::{BTreeMap, BTreeSet},
    fs,
    io::{BufRead, BufReader},
    path::Path,
    process::Command,
};
const ROLES: [&str; 7] = [
    "router",
    "replan",
    "L1-build",
    "L1-other",
    "L2",
    "subagent-fork",
    "subagent-fresh",
];
const TRIGGERS: [&str; 4] = ["msg-in", "coordination tool loop", "beat", "work"];
const FIELDS: [&str; 4] = [
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
];
type Result<T> = std::result::Result<T, Box<dyn std::error::Error>>;

fn instant(s: &str) -> Result<DateTime<Utc>> {
    if let Ok(day) = NaiveDate::parse_from_str(s, "%Y-%m-%d") {
        return Ok(day.and_hms_opt(0, 0, 0).unwrap().and_utc());
    }
    Ok(DateTime::parse_from_rfc3339(s)?.with_timezone(&Utc))
}

#[derive(Clone)]
struct Turn {
    time: DateTime<Utc>,
    weighted: f64,
    trigger: String,
}
#[derive(Default)]
struct Session {
    name: String,
    role: String,
    turns: BTreeMap<String, Turn>,
    fork: bool,
}

fn text(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        Value::Array(a) => a.iter().map(text).collect::<Vec<_>>().join(" "),
        Value::Object(o) => ["text", "content"]
            .iter()
            .filter_map(|k| o.get(*k))
            .map(text)
            .collect::<Vec<_>>()
            .join(" "),
        _ => String::new(),
    }
}

fn stem(name: &str) -> &str {
    name.rsplit_once('-')
        .filter(|(_, n)| !n.is_empty() && n.chars().all(|c| c.is_ascii_digit()))
        .map_or(name, |(prefix, _)| prefix)
}

fn role(name: &str) -> String {
    let lower = stem(name).to_lowercase();
    let has = |words: &[&str]| lower.split('-').any(|s| words.contains(&s));
    if has(&["router", "routing"]) {
        "router"
    } else if has(&["replan", "replanning"]) {
        "replan"
    } else if has(&["l1"]) && has(&["build"]) {
        "L1-build"
    } else if has(&["l1"]) {
        "L1-other"
    } else if has(&["l2"]) {
        "L2"
    } else {
        "unclassified"
    }
    .into()
}

fn read(path: &Path, weights: &[f64; 4], malformed: &mut u64) -> Result<Session> {
    let mut session = Session {
        role: "unclassified".into(),
        name: "other".into(),
        ..Session::default()
    };
    let mut pending = "work".to_string();
    let mut coordination = false;
    for line in BufReader::new(fs::File::open(path)?).lines() {
        let line = line?;
        let parsed = (|| -> Result<()> {
            let v: Value = serde_json::from_str(&line)?;
            let kind = v["type"].as_str().ok_or("missing type")?;
            session.fork |= v["forkContext"].as_bool() == Some(true);
            if let Some(name) = v["agentName"].as_str().or(v["customTitle"].as_str()) {
                session.role = role(name);
                // Only fleet-style names are printed; free-form titles are bucketed.
                let s = stem(name);
                let fleet = |c: char| c.is_ascii_alphanumeric() || c == '-';
                session.name = if (1..=40).contains(&s.len()) && s.chars().all(fleet) {
                    s
                } else {
                    "other"
                }
                .into();
            }
            let content = &v["message"]["content"];
            if kind == "user" || kind == "tool_result" {
                let explicit = v["trigger"].as_str();
                let body = text(content).to_lowercase();
                let tool = kind == "tool_result"
                    || content
                        .as_array()
                        .is_some_and(|a| a.iter().any(|b| b["type"] == "tool_result"));
                pending = if let Some(t) = explicit {
                    if TRIGGERS.contains(&t) {
                        t
                    } else {
                        "unclassified"
                    }
                } else if tool {
                    if coordination {
                        "coordination tool loop"
                    } else {
                        "work"
                    }
                } else if body.starts_with("beat:") || body.starts_with("[beat]") {
                    "beat"
                } else if body.starts_with("work:") {
                    "work"
                } else {
                    "msg-in"
                }
                .into();
            }
            if kind != "assistant" {
                return Ok(());
            }
            let m = &v["message"];
            let id = m["id"].as_str().ok_or("missing message id")?;
            let time = instant(v["timestamp"].as_str().ok_or("missing timestamp")?)?;
            let usage = m["usage"].as_object().ok_or("missing usage")?;
            let mut weighted = 0.;
            for (field, weight) in FIELDS.iter().zip(weights) {
                let count = usage
                    .get(*field)
                    .map_or(Some(0.), Value::as_f64)
                    .ok_or("invalid count")?;
                if !count.is_finite() || count < 0. {
                    return Err("invalid count".into());
                }
                weighted += count * weight;
            }
            if !weighted.is_finite() {
                return Err("invalid weighted count".into());
            }
            // Content-block updates replace usage and time, retaining the initiating trigger.
            let trigger = session
                .turns
                .get(id)
                .map_or_else(|| pending.clone(), |old| old.trigger.clone());
            let previous_coordination = coordination && session.turns.contains_key(id);
            session.turns.insert(
                id.into(),
                Turn {
                    time,
                    weighted,
                    trigger,
                },
            );
            coordination = content.as_array().is_some_and(|a| {
                a.iter().any(|b| {
                    b["type"] == "tool_use" && b["name"].as_str().is_some_and(|n| {
                        "SendMessage TaskList TaskGet TaskUpdate TaskCreate TeamCreate TeamDelete"
                            .split_whitespace()
                            .any(|tool| tool == n)
                            || n.contains("coordination")
                    })
                })
            });
            coordination |= previous_coordination;
            pending = "work".into();
            Ok(())
        })();
        if parsed.is_err() {
            *malformed += 1;
        }
    }
    Ok(session)
}

fn files(path: &Path) -> Result<Vec<std::path::PathBuf>> {
    if !path.exists() {
        return Ok(vec![]);
    }
    let mut paths = fs::read_dir(path)?
        .map(|e| e.map(|e| e.path()))
        .collect::<std::io::Result<Vec<_>>>()?;
    paths.sort();
    Ok(paths)
}

fn census(
    root: &Path,
    start: DateTime<Utc>,
    end: DateTime<Utc>,
    weights: &[f64; 4],
    merges: &Value,
) -> Result<Value> {
    let mut groups: BTreeMap<String, Vec<Turn>> =
        ROLES.iter().map(|s| (s.to_string(), vec![])).collect();
    let mut malformed = 0;
    let mut inherited = 0;
    let mut unclassified = BTreeMap::<String, u64>::new();
    for project in files(root)?.into_iter().filter(|p| p.is_dir()) {
        for path in files(&project)?
            .into_iter()
            .filter(|p| p.extension().is_some_and(|e| e == "jsonl"))
        {
            let parent = read(&path, weights, &mut malformed)?;
            if parent.role == "unclassified" {
                *unclassified.entry(parent.name.clone()).or_default() += 1;
            }
            for turn in parent.turns.values() {
                groups
                    .entry(parent.role.clone())
                    .or_default()
                    .push(turn.clone());
            }
            for child in files(&path.with_extension("").join("subagents"))?
                .into_iter()
                .filter(|p| p.extension().is_some_and(|e| e == "jsonl"))
            {
                let session = read(&child, weights, &mut malformed)?;
                let fork =
                    session.fork || session.turns.keys().any(|id| parent.turns.contains_key(id));
                let name = if fork {
                    "subagent-fork"
                } else {
                    "subagent-fresh"
                };
                for (id, turn) in session.turns {
                    if parent.turns.contains_key(&id) {
                        inherited += 1;
                    } else {
                        groups.get_mut(name).unwrap().push(turn);
                    }
                }
            }
        }
    }
    if groups
        .values()
        .flatten()
        .all(|t| t.time < start || t.time >= end)
    {
        return Err("no census turns".into());
    }
    let mut daily = BTreeMap::new();
    let mut day = start.date_naive();
    while day.and_hms_opt(0, 0, 0).unwrap().and_utc() < end {
        daily.insert(day.to_string(), 0_u64);
        day = day.succ_opt().ok_or("date overflow")?;
    }
    let mut seen = BTreeSet::new();
    for pr in merges.as_array().ok_or("merged PRs must be an array")? {
        let number = pr["number"].as_u64().ok_or("missing PR number")?;
        let time = instant(pr["mergedAt"].as_str().ok_or("missing merge time")?)?;
        if start <= time && time < end && seen.insert(number) {
            *daily.get_mut(&time.date_naive().to_string()).unwrap() += 1;
        }
    }
    let merged = seen.len();
    let mut streak = 0;
    let mut revert = false;
    for (day, count) in &daily {
        let midnight = instant(day)?;
        // Partial UTC days cannot establish a throughput regression.
        if midnight < start || midnight + Duration::days(1) > end || *count >= 58 {
            streak = 0;
        } else {
            streak += 1;
            revert |= streak >= 3;
        }
    }
    let roles: BTreeMap<_, _> = groups.into_iter().map(|(name, turns)| {
        let turns: Vec<_> = turns.into_iter().filter(|t| start <= t.time && t.time < end).collect();
        let weighted: f64 = turns.iter().map(|t| t.weighted).sum();
        let mut counts: BTreeMap<String, usize> = TRIGGERS.iter().map(|t| (t.to_string(), 0)).collect();
        for t in &turns { *counts.entry(t.trigger.clone()).or_default() += 1; }
        let shares: BTreeMap<_, _> = counts.iter().map(|(k, n)| (k, if turns.is_empty() { 0. } else { *n as f64 / turns.len() as f64 })).collect();
        (name, json!({"turns": turns.len(), "weighted_tokens": weighted, "trigger_turns": counts,
            "trigger_share": shares, "weighted_tokens_per_merged_pr": if merged == 0 { None } else { Some(weighted / merged as f64) }}))
    }).collect();
    Ok(
        json!({"since": start.to_rfc3339(), "until": end.to_rfc3339(), "roles": roles,
        "merged_prs": merged, "merged_prs_per_day": daily, "malformed_lines": malformed,
        "inherited_turns_excluded": inherited, "unclassified_sessions": unclassified,
        "guard": {"minimum_merged_prs_per_day": 58, "consecutive_days": 3, "revert_required": revert},
        "denominator": "all repository PRs merged in the same UTC window; role costs include unmerged work",
        "visibility": "local transcripts only; weights are policy assumptions; fork inferred from shared parent message IDs or forkContext"}),
    )
}

fn run() -> Result<Value> {
    let mut args = std::env::args().skip(1);
    let mut options = BTreeMap::new();
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--census" | "--json" => {}
            "--projects" | "--since" | "--until" | "--merged-prs" => {
                options.insert(arg, args.next().ok_or("missing option value")?);
            }
            _ => return Err("unknown census option".into()),
        }
    }
    let end = options
        .get("--until")
        .map_or_else(|| Ok(Utc::now()), |s| instant(s))?;
    let since = options.get("--since").map_or("7d", String::as_str);
    let start = if let Some(days) = since.strip_suffix('d') {
        let days: i64 = days.parse()?;
        if !(1..=3660).contains(&days) {
            return Err("invalid relative window".into());
        }
        end.checked_sub_signed(Duration::days(days))
            .ok_or("date overflow")?
    } else {
        instant(since)?
    };
    if end <= start || end - start > Duration::days(3660) {
        return Err("invalid window".into());
    }
    let root = options
        .get("--projects")
        .map(std::path::PathBuf::from)
        .unwrap_or_else(|| {
            std::path::PathBuf::from(std::env::var_os("HOME").unwrap_or_default())
                .join(".claude/projects")
        });
    if !root.is_dir() {
        return Err("projects unavailable".into());
    }
    let policy: Value =
        serde_json::from_str(include_str!("../../../tools/claude_usage_policy.json"))?;
    let mut weights = [0.; 4];
    for (i, field) in FIELDS.iter().enumerate() {
        weights[i] = policy["weights"][field].as_f64().ok_or("invalid policy")?;
    }
    let merges = if let Some(path) = options.get("--merged-prs") {
        serde_json::from_str(&fs::read_to_string(path)?)?
    } else {
        let search = format!(
            "merged:>={} merged:<{}",
            start.format("%Y-%m-%d"),
            (end.date_naive().succ_opt().ok_or("date overflow")?)
        );
        let query = "query($search:String!,$endCursor:String){search(query:$search,type:ISSUE,first:100,after:$endCursor){issueCount nodes{... on PullRequest{number mergedAt}}pageInfo{hasNextPage endCursor}}}";
        let repo = Command::new("gh")
            .args("repo view --json nameWithOwner --jq .nameWithOwner".split_whitespace())
            .output()?;
        if !repo.status.success() {
            return Err("gh repository lookup failed".into());
        }
        let repository = String::from_utf8(repo.stdout)?;
        let search = format!("repo:{} is:pr is:merged {}", repository.trim(), search);
        let output = Command::new("gh")
            .args(["api", "graphql", "--paginate"])
            .args([
                "-f",
                &format!("query={query}"),
                "-f",
                &format!("search={search}"),
            ])
            .output()?;
        if !output.status.success() {
            return Err("gh merged PR query failed".into());
        }
        let mut rows = Vec::new();
        // `gh --paginate` prints one JSON document per page, back to back.
        for page in serde_json::Deserializer::from_slice(&output.stdout).into_iter::<Value>() {
            let page = page?;
            if page["data"]["search"]["issueCount"]
                .as_u64()
                .is_none_or(|n| n > 1000)
                || page.get("errors").is_some()
            {
                return Err("gh GraphQL errors".into());
            }
            rows.extend(
                page["data"]["search"]["nodes"]
                    .as_array()
                    .ok_or("invalid gh result")?
                    .iter()
                    .cloned(),
            );
        }
        Value::Array(rows)
    };
    census(&root, start, end, &weights, &merges)
}

fn main() {
    match run() {
        Ok(report) => println!("{report}"),
        Err(_) => {
            println!(
                "{}",
                json!({"status": "no result", "reason": "unavailable or invalid census input (including gh)"})
            );
            std::process::exit(3);
        }
    }
}

#[cfg(test)]
mod tests;
