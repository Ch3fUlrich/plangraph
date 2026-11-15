use pg_mainff::{probe, process::System};
use serde_json::json;
fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let result = match args.first().map(String::as_str) {
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
