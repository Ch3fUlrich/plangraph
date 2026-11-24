use std::{env, fs};

fn run() -> Result<(), String> {
    let arguments: Vec<_> = env::args().skip(1).collect();
    let since = match arguments.as_slice() {
        [flag] if flag == "--keeper-only" => None,
        [flag, option, since] if flag == "--keeper-only" && option == "--since" => {
            Some(since.as_str())
        }
        _ => return Err("usage: plangraph baseline --keeper-only [--since RFC3339]".into()),
    };
    if env::var("PG_KEEPER_BACKEND").is_ok_and(|backend| backend.eq_ignore_ascii_case("sqlite")) {
        return Err("keeper baseline requires PostgreSQL".into());
    }
    let path = env::var_os("PG_TELEMETRY_DSN_FILE").ok_or("PG_TELEMETRY_DSN_FILE is required")?;
    let dsn = fs::read_to_string(path).map_err(|_| "cannot read the telemetry reader DSN file")?;
    let schema = env::var("PG_SCHEMA").unwrap_or_else(|_| "keeper".into());
    let report = pg_telemetry::report(dsn.trim(), &schema, since)?;
    println!(
        "{}",
        serde_json::to_string_pretty(&report).map_err(|_| "cannot format baseline report")?
    );
    Ok(())
}

fn main() -> std::process::ExitCode {
    match run() {
        Ok(()) => std::process::ExitCode::SUCCESS,
        Err(reason) => {
            eprintln!("baseline: {reason}");
            std::process::ExitCode::FAILURE
        }
    }
}
