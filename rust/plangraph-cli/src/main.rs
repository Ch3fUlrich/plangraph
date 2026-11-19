use plangraph_cli::{base_fresh, env, feeder, setup};
use std::path::PathBuf;

fn main() {
    std::process::exit(run());
}
fn run() -> i32 {
    let args: Vec<String> = std::env::args().skip(1).collect();
    if args.first().map(String::as_str) == Some("fast-checks") {
        return plangraph_cli::fast_checks::run(&args[1..]);
    }
    if args.first().map(String::as_str) == Some("host-sync") {
        let rest: Vec<std::ffi::OsString> = std::env::args_os().skip(2).collect();
        return plangraph_cli::host_sync::run_cli(&rest);
    }
    if let Some(code) = plangraph_cli::watchers::cli(&args) {
        return code;
    }
    if let [command, path] = args.as_slice() {
        if command == "batch-merge-check" {
            return match plangraph_cli::batch_merge::run(std::path::Path::new(path)) {
                Ok(message) => {
                    println!("{message}");
                    0
                }
                Err(reason) => {
                    eprintln!("{reason}");
                    1
                }
            };
        }
    }
    if args == ["--version"] {
        println!("plangraph {}", env!("CARGO_PKG_VERSION"));
        return 0;
    }
    if let [command, repo] = args.as_slice() {
        if command == "base-fresh" {
            return base_fresh::run(repo);
        }
    }
    let home = std::env::var_os("HOME")
        .or_else(|| std::env::var_os("USERPROFILE"))
        .map(PathBuf::from);
    let Some(home) = home else {
        eprintln!("home unavailable");
        return 2;
    };
    let env_path = std::env::var_os("PLANGRAPH_ENV_FILE")
        .map(PathBuf::from)
        .unwrap_or_else(|| home.join(".config/plangraph/plangraph.env"));
    let effective = match env::load(&env_path, std::env::vars()) {
        Ok(values) => values,
        Err(error) => {
            eprintln!("{error}");
            return 2;
        }
    };
    if args.first().is_some_and(|s| s == "feed") {
        return feeder::run(&home, &effective, &args[1..]);
    }
    if args == ["env", "check"] {
        println!("environment checked {} keys", effective.len());
        return 0;
    }
    if args.first().is_some_and(|arg| arg == "runner") {
        return plangraph_cli::runner::run(&home, &args[1..]);
    }
    if args.first().is_some_and(|arg| arg == "ci-watch") {
        return match plangraph_cli::ci_watch::run(&home, &effective, &args[1..]) {
            Ok(()) => 0,
            Err(error) => {
                eprintln!("ci-watch: {error}");
                2
            }
        };
    }
    let mode = match args
        .iter()
        .map(String::as_str)
        .collect::<Vec<_>>()
        .as_slice()
    {
        ["setup", "apply"] => setup::Mode::Apply,
        ["setup", "apply", "--dry-run"] => setup::Mode::DryRun,
        ["setup", "check"] => setup::Mode::Check,
        _ => {
            eprintln!(
                "usage: plangraph setup apply [--dry-run] | setup check | env check | base-fresh <repo> | batch-merge-check FACTS.json | ci-watch [--dry-run] [--keeper] | runner plan|install|check | feed --once [--dry-run] | host-sync [--data-dir PATH] [--bin-dir PATH] | --version"
            );
            return 2;
        }
    };
    let (code, rows) = setup::run(&home, setup::MANIFEST, mode);
    for row in rows {
        println!("{row}");
    }
    code
}
