//! `plangraph runner plan|install|check` (Linux CI host side).
mod check;
mod config;
pub(crate) mod download;
mod observe;
mod plan;
mod steps;
mod template;

use std::path::{Path, PathBuf};

const USAGE: &str = "usage: plangraph runner plan|install --os linux --count N [--config FILE] | runner check [--count N] [--config FILE]";

struct Args {
    os: Option<String>,
    count: Option<u32>,
    config: Option<PathBuf>,
}

fn parse(rest: &[String]) -> Result<Args, String> {
    let mut a = Args {
        os: None,
        count: None,
        config: None,
    };
    let mut it = rest.iter();
    while let Some(flag) = it.next() {
        let value = it.next().ok_or_else(|| format!("{flag} needs a value"))?;
        match flag.as_str() {
            "--os" => a.os = Some(value.clone()),
            "--count" => a.count = Some(value.parse().map_err(|_| "--count must be a number")?),
            "--config" => a.config = Some(value.into()),
            other => return Err(format!("unknown flag {other}")),
        }
    }
    Ok(a)
}

pub fn run(home: &Path, args: &[String]) -> i32 {
    match run_inner(home, args) {
        Ok(code) => code,
        Err(e) => {
            eprintln!("runner: {e}");
            2
        }
    }
}

/// Host and path only: userinfo and query strings can carry credentials.
pub fn redact_url(url: &str) -> String {
    let rest = url.split_once("://").map_or(url, |(_, r)| r);
    let rest = rest.split(['?', '#']).next().unwrap_or("");
    let (authority, path) = rest.split_once('/').unwrap_or((rest, ""));
    let host = authority.rsplit('@').next().unwrap_or("");
    format!("{host}/{path}")
}

fn run_inner(home: &Path, args: &[String]) -> Result<i32, String> {
    let (verb, rest) = args.split_first().ok_or(USAGE)?;
    let a = parse(rest).map_err(|e| format!("{e}\n{USAGE}"))?;
    let verb = verb.as_str();
    if !matches!(verb, "plan" | "install" | "check") {
        return Err(USAGE.into());
    }
    if verb != "check" {
        match a.os.as_deref() {
            Some("linux") => {}
            Some("windows") => return Err("--os windows is not implemented yet (slice S2)".into()),
            _ => return Err(format!("--os linux is required\n{USAGE}")),
        }
    }
    let config_path = a
        .config
        .unwrap_or_else(|| home.join(".config/plangraph/ci-runners.toml"));
    let cfg = config::Config::load(&config_path)?;
    let count = a
        .count
        .or(cfg.count)
        .filter(|c| (1..=64).contains(c))
        .ok_or("--count N (1..=64) is required")?;
    if verb == "check" {
        require_root().map_err(|_| "check must run as root inside the CI host")?;
        let mut checks: Vec<_> = cfg
            .user_names(count)
            .iter()
            .flat_map(|u| check::evaluate(&check::probe(&cfg, u)))
            .collect();
        checks.push(check::proc_hidepid_check(
            &std::fs::read_to_string("/proc/mounts").unwrap_or_default(),
        ));
        print!("{}", check::render(&checks));
        return Ok(i32::from(check::failed(&checks)));
    }
    let state = observe::observe(&cfg, count, Path::new("/"))?;
    let actions = plan::plan(&cfg, &state)?;
    if verb == "plan" {
        if actions.is_empty() {
            println!("nothing to do");
        }
        for (i, a) in actions.iter().enumerate() {
            println!("{:>2}. {}", i + 1, a.describe());
        }
        return Ok(0);
    }
    require_root()?;
    for a in &actions {
        println!("-> {}", a.describe());
        for step in steps::steps_for(&cfg, a) {
            steps::execute(&step).map_err(|e| format!("{}: {e}", a.describe()))?;
        }
    }
    println!(
        "{} actions applied; run `plangraph runner check` next",
        actions.len()
    );
    Ok(0)
}

fn require_root() -> Result<(), String> {
    let out = std::process::Command::new("id")
        .arg("-u")
        .output()
        .map_err(|e| format!("id: {e}"))?;
    if String::from_utf8_lossy(&out.stdout).trim() == "0" {
        Ok(())
    } else {
        Err("install must run as root inside the CI host".into())
    }
}
