//! `plangraph flow-snapshot [--json] [--out PATH] [--readings FILE]`: write the file the
//! flow-watchdog checker reads. A failed reading is reported on stderr and its fields are
//! left out of the file, so the checker raises its durable invalid-snapshot alert.
mod live;

use pg_worker_core::flow_collect::{assemble_with_gaps, decode};
use serde_json::Value;
use std::{collections::BTreeMap, path::PathBuf};

const USAGE: &str = "usage: plangraph flow-snapshot [--json] [--out PATH] [--readings FILE]";

struct Options {
    json: bool,
    out: Option<PathBuf>,
    readings: Option<PathBuf>,
}

fn options(args: &[String]) -> Result<Options, String> {
    let mut options = Options {
        json: false,
        out: None,
        readings: None,
    };
    let mut it = args.iter();
    while let Some(arg) = it.next() {
        match arg.as_str() {
            "--json" => options.json = true,
            "--out" => options.out = Some(it.next().ok_or(USAGE)?.into()),
            "--readings" => options.readings = Some(it.next().ok_or(USAGE)?.into()),
            _ => return Err(USAGE.into()),
        }
    }
    Ok(options)
}

fn default_out(env: &BTreeMap<String, String>) -> Result<PathBuf, String> {
    Ok(live::state_dir(env)?.join("flow-snapshot.json"))
}

/// Collect, assemble and write; returns the process exit code (3: written with gaps).
pub fn run(env: &BTreeMap<String, String>, args: &[String]) -> Result<i32, String> {
    let options = options(args)?;
    let readings: Value = match &options.readings {
        Some(path) => {
            serde_json::from_slice(&std::fs::read(path).map_err(|e| format!("readings: {e}"))?)
                .map_err(|e| format!("readings: {e}"))?
        }
        None => live::collect(env),
    };
    // A malformed source is a gap of that source; the file is still replaced.
    let (readings, malformed) = decode(readings)?;
    let assembled = assemble_with_gaps(&readings, malformed);
    for gap in &assembled.gaps {
        eprintln!("flow-snapshot: {gap}");
    }
    let text = serde_json::to_string(&assembled.snapshot).map_err(|e| e.to_string())?;
    let out = match options.out {
        Some(out) => out,
        None => default_out(env)?,
    };
    if let Some(parent) = out.parent() {
        std::fs::create_dir_all(parent).map_err(|e| e.to_string())?;
    }
    crate::atomic::write(&out, text.as_bytes()).map_err(|e| format!("{}: {e}", out.display()))?;
    if options.json {
        println!("{text}");
    }
    Ok(if assembled.gaps.is_empty() { 0 } else { 3 })
}

pub fn cli(args: &[String]) -> i32 {
    if args == ["--help"] || args == ["-h"] {
        println!("{USAGE}");
        return 0;
    }
    let result = crate::env::default_path()
        .and_then(|path| crate::env::load(&path, std::env::vars()))
        .and_then(|env| run(&env, args));
    result.unwrap_or_else(|error| {
        eprintln!("flow-snapshot: {error}");
        2
    })
}
