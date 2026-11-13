use plangraph_cli::{env, setup};
use std::path::PathBuf;

fn main() {
    std::process::exit(run());
}
fn run() -> i32 {
    let args: Vec<String> = std::env::args().skip(1).collect();
    if args == ["--version"] {
        println!("plangraph {}", env!("CARGO_PKG_VERSION"));
        return 0;
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
    if args == ["env", "check"] {
        println!("environment checked {} keys", effective.len());
        return 0;
    }
    // Startup is single-threaded; all command-specific threads start below.
    for (key, value) in effective {
        std::env::set_var(key, value);
    }
    if args.first().is_some_and(|a| a == "unit-breaker") {
        return plangraph_cli::breaker::cli(&home, &args[1..]);
    }
    if args.first().is_some_and(|a| a == "migrate-breaker") {
        if args.len() > 2 || (args.len() == 2 && args[1] != "--dry-run") {
            return 2;
        }
        let new = plangraph_cli::paths::state(&home);
        let old = new.parent().unwrap().join("autoos/breaker");
        return match plangraph_cli::migrate::breaker(&old, &new.join("breaker"), args.len() == 2) {
            Ok(count) => {
                println!("breaker migration files={count}");
                0
            }
            Err(_) => {
                eprintln!("breaker migration refused");
                2
            }
        };
    }
    if args.first().is_some_and(|a| a == "taildrop-sort") {
        return plangraph_cli::taildrop::cli(&home, &args[1..]);
    }
    if args.first().is_some_and(|a| a == "cost-gate") {
        return plangraph_cli::cost::cli(&home, &args[1..]);
    }
    if args.first().is_some_and(|a| a == "namespace") {
        return plangraph_cli::namespace::cli(&home, &args[1..]);
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
                "usage: plangraph setup apply [--dry-run] | setup check | env check | --version"
            );
            return 2;
        }
    };
    let binary = std::env::current_exe().unwrap();
    let (code, rows) = setup::run(&home, &binary, setup::MANIFEST, mode);
    for row in rows {
        println!("{row}");
    }
    code
}
