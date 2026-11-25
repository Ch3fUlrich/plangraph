use std::{env, fs};

/// What the command line asks for.
#[derive(Debug, PartialEq)]
enum Request<'a> {
    /// `--version`: the host smoke check after an install.
    Version,
    Report {
        since: Option<&'a str>,
    },
}

fn parse(arguments: &[String]) -> Result<Request<'_>, String> {
    match arguments {
        [flag] if flag == "--version" => Ok(Request::Version),
        [flag] if flag == "--keeper-only" => Ok(Request::Report { since: None }),
        [flag, option, since] if flag == "--keeper-only" && option == "--since" => {
            Ok(Request::Report {
                since: Some(since.as_str()),
            })
        }
        _ => Err("usage: plangraph baseline --keeper-only [--since RFC3339]".into()),
    }
}

fn run() -> Result<(), String> {
    let arguments: Vec<_> = env::args().skip(1).collect();
    let since = match parse(&arguments)? {
        Request::Version => {
            println!("pg-telemetry {}", env!("CARGO_PKG_VERSION"));
            return Ok(());
        }
        Request::Report { since } => since,
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

#[cfg(test)]
mod tests {
    use super::*;

    fn args(list: &[&str]) -> Vec<String> {
        list.iter().map(|a| a.to_string()).collect()
    }

    #[test]
    fn version_is_a_request_not_a_usage_error() {
        assert_eq!(parse(&args(&["--version"])), Ok(Request::Version));
        assert_eq!(
            parse(&args(&["--keeper-only"])),
            Ok(Request::Report { since: None })
        );
        assert_eq!(
            parse(&args(&["--keeper-only", "--since", "2026-01-01T00:00:00Z"])),
            Ok(Request::Report {
                since: Some("2026-01-01T00:00:00Z")
            })
        );
        assert!(parse(&args(&["--version", "extra"])).is_err());
        assert!(parse(&args(&[])).is_err());
    }
}
