use pg_worker_core::{
    host_readiness::{self, Native, Record, WRITER_SANDBOX},
    hosts::HostRegistry,
};
use std::{
    env, io,
    path::PathBuf,
    time::{SystemTime, UNIX_EPOCH},
};
fn run() -> io::Result<bool> {
    let mut args = env::args().skip(1);
    let first = args.next();
    if first.as_deref() == Some("--child-start") {
        return Ok(true);
    }
    if first.as_deref() == Some("--child-commit") {
        host_readiness::commit_child(&env::current_dir()?)?;
        return Ok(true);
    }
    let home = env::var_os("CODEX_HOME")
        .map(PathBuf::from)
        .or_else(|| env::var_os("HOME").map(|p| PathBuf::from(p).join(".codex")))
        .ok_or_else(|| io::Error::other("CODEX_HOME or HOME required"))?;
    let mut project = env::current_dir()?;
    let mut sandbox = WRITER_SANDBOX.to_string();
    let mut registry = None;
    let mut state = None;
    let mut codex = PathBuf::from("codex");
    let mut scratch_root = env::temp_dir();
    let mut args = first.into_iter().chain(args);
    while let Some(flag) = args.next() {
        let value = args
            .next()
            .ok_or_else(|| io::Error::other("missing option value"))?;
        match flag.as_str() {
            "--project" => project = value.into(),
            "--registry" => registry = Some(PathBuf::from(value)),
            "--state" => state = Some(PathBuf::from(value)),
            "--sandbox" => sandbox = value,
            "--codex" => codex = value.into(),
            "--scratch-root" => scratch_root = value.into(),
            _ => return Err(io::Error::other("unknown option")),
        }
    }
    if !host_readiness::valid_sandbox(&sandbox) {
        return Err(io::Error::other("invalid sandbox mode"));
    }
    let destination = match (registry, state) {
        (Some(registry), Some(state)) => {
            let registry = HostRegistry::load(&registry).map_err(io::Error::other)?;
            Some((registry.self_host().id.clone(), state))
        }
        (None, None) => None,
        _ => {
            return Err(io::Error::other(
                "--registry and --state must be provided together",
            ))
        }
    };
    let errors = match host_readiness::settings(&home, &project, sandbox.clone()) {
        Ok(settings) => {
            let scratch = tempfile::tempdir_in(scratch_root)?;
            let mut runner = Native {
                codex,
                executable: env::current_exe()?,
            };
            host_readiness::check(&settings, scratch.path(), &mut runner)
        }
        Err(_) => vec!["readiness configuration unavailable".into()],
    };
    if let Some((host, state)) = destination {
        Record {
            host,
            sandbox,
            checked_at: SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .map_err(io::Error::other)?
                .as_secs(),
            errors: errors.clone(),
        }
        .save(&state)?;
    }
    if errors.is_empty() {
        println!("READY");
    } else {
        for error in &errors {
            println!("MISSING {error}");
        }
    }
    Ok(errors.is_empty())
}
fn main() -> std::process::ExitCode {
    match run() {
        Ok(true) => 0,
        Ok(false) => 1,
        Err(e) => {
            eprintln!("MISSING readiness configuration: {e}");
            1
        }
    }
    .into()
}
