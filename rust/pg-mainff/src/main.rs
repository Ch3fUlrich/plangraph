use pg_mainff::{advance, probe, process::System};
use serde_json::json;
fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    if args.first().map(String::as_str) == Some("main-ff") {
        let mut repo = None;
        let mut state = None;
        let mut unit = "plangraph-keeper.service";
        let mut fetch = true;
        let mut i = 1;
        while i < args.len() {
            match args[i].as_str() {
                "--repo" if i + 1 < args.len() => {
                    i += 1;
                    repo = Some(std::path::Path::new(&args[i]));
                }
                "--keeper-state" if i + 1 < args.len() => {
                    i += 1;
                    state = Some(std::path::Path::new(&args[i]));
                }
                "--keeper-unit" if i + 1 < args.len() => {
                    i += 1;
                    unit = &args[i];
                }
                "--no-fetch" => fetch = false,
                _ => {
                    eprintln!("main-ff: invalid argument");
                    std::process::exit(2);
                }
            }
            i += 1;
        }
        let (Some(repo), Some(state)) = (repo, state) else {
            eprintln!("main-ff: --repo and --keeper-state required");
            std::process::exit(2);
        };
        let (code, line) = advance::main_ff(&System, repo, state, fetch, unit);
        println!("{line}");
        std::process::exit(code);
    }
    let result = match args.first().map(String::as_str) {
        Some("status") if args.len() == 2 => Ok(json!(advance::status(
            &System,
            std::path::Path::new(&args[1])
        ))),
        Some("probe") => probe::main_pid(
            &System,
            args.get(1)
                .map(String::as_str)
                .unwrap_or("plangraph-keeper.service"),
        )
        .map(|pid| json!(pid)),
        Some("running") if args.len() == 2 => {
            probe::running(std::path::Path::new(&args[1]), &System, cfg!(windows))
                .map(|sha| json!(sha))
        }
        _ => Err("usage: pg-mainff probe [UNIT] | running STATE".into()),
    };
    match result {
        Ok(value) => println!("{value}"),
        Err(error) => {
            eprintln!("{error}");
            std::process::exit(2);
        }
    }
}
