//! CI helper for flaky-test handling and shard timing (LE-CI-FLAKE-RETRY-QUARANTINE-WIRING,
//! LE-CI-TRAIN-CACHE-PARTITION-ARTIFACT). Usage:
//!   ci-flake failed-ids LOG                      failed test ids of a pytest or cargo log, one per line
//!   ci-flake classify --suite S --runner R --run-id N --attempt N --sha SHA --first LOG --second LOG --out DIR
//!   ci-flake quarantine --ledger FILE --as-of ISO   markdown of the active owners and expiries; writes active=N
//!   ci-flake cargo-quarantine --ledger FILE --as-of ISO --mode blocking|quarantine
//!   ci-flake combine-durations --out FILE IN...
//!   ci-flake cargo-test --package P [--ledger FILE --as-of ISO] [--quarantine-only 1] [--log FILE]
mod cargo_test;
mod classify;
mod durations;
mod ledger;
mod logs;

use std::collections::HashMap;
use std::io::Write;

fn options(args: &[String]) -> Result<(HashMap<String, String>, Vec<String>), String> {
    let (mut named, mut rest) = (HashMap::new(), Vec::new());
    let mut it = args.iter();
    while let Some(arg) = it.next() {
        match arg.strip_prefix("--") {
            Some(name) => {
                let value = it.next().ok_or(format!("--{name} needs a value"))?;
                named.insert(name.to_string(), value.clone());
            }
            None => rest.push(arg.clone()),
        }
    }
    Ok((named, rest))
}

fn need<'a>(named: &'a HashMap<String, String>, name: &str) -> Result<&'a str, String> {
    named
        .get(name)
        .map(String::as_str)
        .ok_or(format!("missing --{name}"))
}

fn read(path: &str) -> Result<String, String> {
    std::fs::read_to_string(path).map_err(|e| format!("{path}: {e}"))
}

fn load_ledger(
    named: &HashMap<String, String>,
) -> Result<(Vec<ledger::Entry>, chrono::DateTime<chrono::FixedOffset>), String> {
    let as_of = ledger::parse_as_of(need(named, "as-of")?)?;
    Ok((ledger::parse(&read(need(named, "ledger")?)?)?, as_of))
}

fn output(line: &str) {
    if let Some(path) = std::env::var_os("GITHUB_OUTPUT") {
        if let Ok(mut file) = std::fs::OpenOptions::new()
            .append(true)
            .create(true)
            .open(path)
        {
            let _ = writeln!(file, "{line}");
        }
    }
}

fn run(args: &[String]) -> Result<(), String> {
    let args: Vec<String> = args
        .iter()
        .filter(|a| *a != "--no-fail-fast")
        .cloned()
        .collect();
    let (named, rest) = options(args.get(1..).unwrap_or_default())?;
    match args.first().map(String::as_str) {
        Some("failed-ids") => {
            let log = read(rest.first().ok_or("failed-ids needs a log file")?)?;
            logs::failed_ids(&log)
                .iter()
                .for_each(|id| println!("{id}"));
        }
        Some("classify") => {
            let (first, second) = (need(&named, "first")?, need(&named, "second")?);
            let run = classify::Run {
                suite: need(&named, "suite")?.into(),
                runner: need(&named, "runner")?.into(),
                run_id: need(&named, "run-id")?.into(),
                attempt: need(&named, "attempt")?.into(),
                sha: need(&named, "sha")?.into(),
                first_log: first.into(),
                second_log: second.into(),
            };
            let record = classify::record(&run, &read(first)?, &read(second)?);
            let out = std::path::Path::new(need(&named, "out")?);
            std::fs::create_dir_all(out).map_err(|e| e.to_string())?;
            let write = |name: &str, value: &serde_json::Value| {
                std::fs::write(
                    out.join(name),
                    serde_json::to_string_pretty(value).unwrap() + "\n",
                )
                .map_err(|e| e.to_string())
            };
            write("flake-record.json", &record)?;
            write(
                "flake-items.json",
                &serde_json::json!({"items": record["keeper_items"]}),
            )?;
            print!("{}", classify::summary(&record));
            output(&format!(
                "verdict={}",
                record["verdict"].as_str().unwrap_or("")
            ));
        }
        Some("quarantine") => {
            let (entries, as_of) = load_ledger(&named)?;
            print!("{}", ledger::summary(&entries, as_of));
            output(&format!("active={}", ledger::active(&entries, as_of).len()));
        }
        Some("cargo-quarantine") => {
            let (entries, as_of) = load_ledger(&named)?;
            let mode = need(&named, "mode")?;
            if mode != "blocking" && mode != "quarantine" {
                return Err("--mode must be blocking or quarantine".into());
            }
            for (package, test) in ledger::cargo_tests(&entries, as_of) {
                println!("{package} {test}");
            }
        }
        Some("cargo-test") => {
            // The ledger comes from the options or, in CI, from the environment the quarantine-ledger action sets.
            let from_env = |name: &str| std::env::var(name).ok();
            let ledger_path = named
                .get("ledger")
                .cloned()
                .or_else(|| from_env("PYTEST_QUARANTINE_FILE"));
            let as_of = named
                .get("as-of")
                .cloned()
                .or_else(|| from_env("PYTEST_QUARANTINE_AS_OF"));
            let package = need(&named, "package")?;
            let names: Vec<String> = match (ledger_path, as_of) {
                (Some(file), Some(at)) => {
                    let (entries, when) =
                        (ledger::parse(&read(&file)?)?, ledger::parse_as_of(&at)?);
                    ledger::cargo_tests(&entries, when)
                        .into_iter()
                        .filter(|(p, _)| p == package)
                        .map(|(_, t)| t)
                        .collect()
                }
                _ => Vec::new(),
            };
            let only = named.contains_key("quarantine-only");
            if only && names.is_empty() {
                println!("no active quarantined tests of {package}");
                return Ok(());
            }
            let code = cargo_test::run(
                &cargo_test::cargo_args(package, &names, only),
                named.get("log").map(std::path::Path::new),
            )?;
            if code != 0 {
                std::process::exit(code); // cargo's own status: the step fails with it
            }
        }
        Some("combine-durations") => {
            let mut inputs = Vec::new();
            for path in &rest {
                inputs.push(durations::parse(&read(path)?).map_err(|e| format!("{path}: {e}"))?);
            }
            let merged = durations::combine(&inputs);
            let text = serde_json::to_string(&merged).unwrap() + "\n";
            std::fs::write(need(&named, "out")?, text).map_err(|e| e.to_string())?;
        }
        _ => return Err(
            "usage: ci-flake failed-ids|classify|quarantine|cargo-quarantine|combine-durations ..."
                .into(),
        ),
    }
    Ok(())
}

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    if let Err(message) = run(&args) {
        eprintln!("ci-flake: {message}");
        std::process::exit(2);
    }
}
