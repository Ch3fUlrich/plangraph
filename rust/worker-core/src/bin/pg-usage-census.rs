use chrono::{DateTime, Duration, Utc};
use pg_worker_core::usage_census::census;
use serde_json::Value;
use std::{env, fs, path::PathBuf, process::Command};
fn run() -> Result<Value, Box<dyn std::error::Error>> {
    let mut projects = PathBuf::from(env::var("HOME")?).join(".claude/projects");
    let mut end = Utc::now();
    let mut since = "7d".to_string();
    let mut cache = None;
    let mut repo = None;
    let mut args = env::args().skip(1);
    while let Some(arg) = args.next() {
        if arg == "--json" {
            continue;
        }
        let value = args.next().ok_or("missing option value")?;
        match arg.as_str() {
            "--projects" => projects = value.into(),
            "--since" => since = value,
            "--until" => end = DateTime::parse_from_rfc3339(&value)?.with_timezone(&Utc),
            "--pr-cache" => cache = Some(value),
            "--repo" => repo = Some(value),
            _ => return Err("unknown census option".into()),
        }
    }
    let start = if let Some(days) = since.strip_suffix('d') {
        let days: i64 = days.parse()?;
        if !(1..=366).contains(&days) {
            return Err("invalid day window".into());
        }
        end - Duration::days(days)
    } else {
        DateTime::parse_from_rfc3339(&since)?.with_timezone(&Utc)
    };
    let prs: Vec<Value> = if let Some(path) = cache {
        serde_json::from_slice(&fs::read(path)?)?
    } else {
        let search = format!("merged:>={}", start.date_naive());
        let mut gh = Command::new("gh");
        gh.args([
            "pr",
            "list",
            "--state",
            "merged",
            "--search",
            &search,
            "--limit",
            "1000",
            "--json",
            "number,mergedAt",
        ]);
        if let Some(repo) = repo {
            gh.args(["--repo", &repo]);
        }
        let result = gh.output()?;
        if !result.status.success() {
            return Err("merged PR collection failed".into());
        }
        let rows: Vec<Value> = serde_json::from_slice(&result.stdout)?;
        if rows.len() >= 1000 {
            return Err("PR query limit reached; provide a complete --pr-cache".into());
        }
        rows
    };
    let report = census(&projects, &start.to_rfc3339(), &end.to_rfc3339(), &prs)?;
    if report["roles"]
        .as_object()
        .ok_or("missing roles")?
        .values()
        .all(|row| row["turns"].as_u64() == Some(0))
    {
        return Err("no transcript usage in window".into());
    }
    Ok(report)
}
fn main() {
    match run() {
        Ok(report) => println!("{report}"),
        Err(_) => {
            eprintln!(
                "census unavailable: check window, transcripts, helper and complete PR evidence"
            );
            std::process::exit(3);
        }
    }
}
