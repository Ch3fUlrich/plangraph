mod router_liveness;

use plangraph_cli::{base_fresh, env, feeder, setup};
use std::path::PathBuf;

mod keeper_restart;

fn main() {
    std::process::exit(run());
}
fn run() -> i32 {
    let args: Vec<String> = std::env::args().skip(1).collect();
    if args.first().is_some_and(|arg| arg == "keeper-outbox") {
        return plangraph_cli::keeper_outbox::run(&args[1..]);
    }
    if args
        .first()
        .is_some_and(|command| command == "keeper-restart")
    {
        return keeper_restart::run(&args[1..]);
    }
    if args.first().map(String::as_str) == Some("fast-checks") {
        return plangraph_cli::fast_checks::run(&args[1..]);
    }
    if args.first().map(String::as_str) == Some("router") {
        return plangraph_cli::router_batch::run(&args[1..]);
    }
    if args.first().map(String::as_str) == Some("batch-size") {
        return plangraph_cli::batch_size::run(&args[1..]);
    }
    if args.first().map(String::as_str) == Some("route-shadow") {
        return plangraph_cli::route_shadow::cli(&args[1..]);
    }
    if args.first().map(String::as_str) == Some("review") {
        return plangraph_cli::review::run(&args[1..]);
    }
    if args.first().map(String::as_str) == Some("host-sync") {
        let rest: Vec<std::ffi::OsString> = std::env::args_os().skip(2).collect();
        return plangraph_cli::host_sync::run_cli(&rest);
    }
    if args.first().map(String::as_str) == Some("install-router-liveness") {
        return router_liveness::run(&args[1..]);
    }
    if args.first().map(String::as_str) == Some("host-link-deliver") {
        return plangraph_cli::host_link_bridge::cli(&args[1..]);
    }
    if args.first().map(String::as_str) == Some("land-timeline") {
        let home = std::env::var_os("HOME")
            .or_else(|| std::env::var_os("USERPROFILE"))
            .map(PathBuf::from)
            .unwrap_or_default();
        return plangraph_cli::land::timeline::run(&home, &args[1..]);
    }
    if args.first().map(String::as_str) == Some("land-train") {
        let home = std::env::var_os("HOME")
            .or_else(|| std::env::var_os("USERPROFILE"))
            .map(PathBuf::from)
            .unwrap_or_default();
        return plangraph_cli::land::cli::run(&home, &args[1..]);
    }
    if args.first().map(String::as_str) == Some("main-state") {
        let home = std::env::var_os("HOME")
            .or_else(|| std::env::var_os("USERPROFILE"))
            .map(PathBuf::from)
            .unwrap_or_default();
        return plangraph_cli::land::main_gh::run(&home, &args[1..]);
    }
    if let Some(code) = plangraph_cli::watchers::cli(&args) {
        return code;
    }
    if let Some(code) = plangraph_cli::wait_pid::cli(&args) {
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
    if args.first().is_some_and(|s| s == "codex-host-readiness") {
        return plangraph_cli::codex_readiness::run(&args[1..]);
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
    if matches!(args.as_slice(), [keeper, events, ..] if keeper == "keeper" && events == "events") {
        return match plangraph_cli::keeper_events::cli(&effective, &args[2..]) {
            Ok(page) => {
                println!(
                    "{}",
                    serde_json::to_string(&page).expect("serializable event page")
                );
                0
            }
            Err(error) => {
                eprintln!("{error}");
                2
            }
        };
    }
    if args.first().is_some_and(|s| s == "state") {
        return match plangraph_cli::state_shadow::run(&home, &effective, &args[1..]) {
            Ok(()) => 0,
            Err(error) => {
                eprintln!("state: {error}");
                2
            }
        };
    }
    if args.first().is_some_and(|s| s == "feed") {
        return feeder::run(&home, &effective, &args[1..]);
    }
    if args.first().is_some_and(|s| s == "quota") {
        return feeder::quota_status(&effective, &args[1..]);
    }
    if args.first().is_some_and(|s| s == "throughput") {
        return plangraph_cli::throughput::run(&home, &effective, &args[1..]);
    }
    if args.first().is_some_and(|s| s == "merge-close") {
        return plangraph_cli::merge_close::run(&effective, &args[1..]);
    }
    if args.first().is_some_and(|s| s == "landing") {
        return plangraph_cli::landing::run(&effective, &args[1..]);
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
    let (mode, component) = match args
        .iter()
        .map(String::as_str)
        .collect::<Vec<_>>()
        .as_slice()
    {
        ["setup", "apply"] => (setup::Mode::Apply, None),
        ["setup", "apply", "--dry-run"] => (setup::Mode::DryRun, None),
        ["setup", "check"] => (setup::Mode::Check, None),
        ["setup", "apply", "--component", name] => (setup::Mode::Apply, Some(*name)),
        ["setup", "apply", "--component", name, "--dry-run"] => (setup::Mode::DryRun, Some(*name)),
        ["setup", "check", "--component", name] => (setup::Mode::Check, Some(*name)),
        _ => {
            eprintln!(
                "usage: plangraph setup apply [--component NAME] [--dry-run] | setup check [--component NAME] | env check | base-fresh <repo> | batch-merge-check FACTS.json | ci-watch [--dry-run] [--keeper] | keeper events [--since SEQ] [--limit N] | state wait --role ROLE [--once] | land-timeline | land-train | main-state | merge-close [--count | --repos] [--project NAME] | review --repo <path> --head <sha> [--base origin/main] [--item <key>] [--focus <file>] | runner plan|install|check | feed --once [--dry-run] | throughput | keeper-restart CHECKOUT KEEPER_CONFIG MIGRATION_DSN_FILE | quota [--db PATH] | wait-pid (--pid PID | --pid-file PATH) [--expect SUBSTR] [--check | --timeout SECS] | host-sync [--data-dir PATH] [--bin-dir PATH] | host-link-deliver | install-router-liveness TARGET CONFIG | --version"
            );
            return 2;
        }
    };
    let (code, rows) = setup::run_selected(&home, setup::MANIFEST, mode, component);
    for row in rows {
        println!("{row}");
    }
    code
}
