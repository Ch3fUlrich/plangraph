use pg_worker_core::host_readiness::{self, Native};
use std::{env, io, path::PathBuf};
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
    let mut sandbox = "workspace-write".to_string();
    let mut codex = PathBuf::from("codex");
    let mut scratch_root = env::temp_dir();
    let mut args = first.into_iter().chain(args);
    while let Some(flag) = args.next() {
        let value = args
            .next()
            .ok_or_else(|| io::Error::other("missing option value"))?;
        match flag.as_str() {
            "--project" => project = value.into(),
            "--sandbox" => sandbox = value,
            "--codex" => codex = value.into(),
            "--scratch-root" => scratch_root = value.into(),
            _ => return Err(io::Error::other("unknown option")),
        }
    }
    if !["workspace-write", "read-only", "danger-full-access"].contains(&sandbox.as_str()) {
        return Err(io::Error::other("invalid sandbox mode"));
    }
    let settings = host_readiness::settings(&home, &project, sandbox)?;
    let scratch = tempfile::tempdir_in(scratch_root)?;
    let mut runner = Native {
        codex,
        executable: env::current_exe()?,
    };
    let errors = host_readiness::check(&settings, scratch.path(), &mut runner);
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
