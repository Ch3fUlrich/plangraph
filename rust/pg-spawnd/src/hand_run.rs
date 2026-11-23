//! Explicit launch entry point for hand-started train writers.
use std::{env, os::unix::process::ExitStatusExt, process::Command};

pub(crate) fn execute(args: &[String]) -> Result<i32, String> {
    let [separator, program, child @ ..] = args else {
        return Err("Usage: pg-spawnd hand-run -- PROGRAM [ARGS ...]".into());
    };
    if separator != "--" {
        return Err("Usage: pg-spawnd hand-run -- PROGRAM [ARGS ...]".into());
    }
    let cwd = env::current_dir().map_err(|error| error.to_string())?;
    let target = pg_worker_core::cargo_cache::require_writer_target(
        &cwd,
        env::var_os("CARGO_TARGET_DIR").as_deref(),
    )
    .map_err(|error| error.to_string())?;
    let _lease =
        pg_worker_core::cargo_cache::writer_lease(&target).map_err(|error| error.to_string())?;
    let status = Command::new(program)
        .args(child)
        .env("CARGO_TARGET_DIR", target)
        .status()
        .map_err(|error| format!("hand-run {program}: {error}"))?;
    Ok(status
        .code()
        .unwrap_or_else(|| 128 + status.signal().unwrap_or(0)))
}
