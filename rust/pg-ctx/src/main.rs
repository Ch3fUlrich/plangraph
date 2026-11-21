use pg_ctx::nav::{self, BuildOptions, NavResult, ScipInput};
use std::path::PathBuf;

const USAGE: &str = "usage: pg-ctx nav <symbol|path|key> | nav-build [--scip FILE | --scip-at PREFIX FILE]... [--links FILE] [--rules FILE]";

fn run() -> NavResult<()> {
    let cwd = std::env::current_dir()?;
    let mut args = std::env::args().skip(1);
    let output = match args.next().as_deref() {
        Some("nav") => {
            let key = args.next().ok_or(USAGE)?;
            if args.next().is_some() || key.is_empty() {
                return Err(USAGE.into());
            }
            serde_json::to_string_pretty(&nav::navigate(&cwd, &key)?)?
        }
        Some("nav-build") => {
            let mut options = BuildOptions::default();
            while let Some(flag) = args.next() {
                let value = args.next().ok_or(USAGE)?;
                match flag.as_str() {
                    "--scip" => options.scip.push(ScipInput {
                        file: PathBuf::from(value),
                        prefix: String::new(),
                    }),
                    "--scip-at" => options.scip.push(ScipInput {
                        file: PathBuf::from(args.next().ok_or(USAGE)?),
                        prefix: value,
                    }),
                    "--links" if options.links.is_none() => options.links = Some(value.into()),
                    "--rules" if options.rules.is_none() => options.rules = Some(value.into()),
                    _ => return Err(USAGE.into()),
                }
            }
            serde_json::to_string_pretty(&nav::build(&cwd, &options)?)?
        }
        Some("--help" | "-h") | None => USAGE.to_owned(),
        _ => return Err(USAGE.into()),
    };
    println!("{output}");
    Ok(())
}

fn main() -> std::process::ExitCode {
    match run() {
        Ok(()) => std::process::ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("pg-ctx: {error}");
            std::process::ExitCode::FAILURE
        }
    }
}
