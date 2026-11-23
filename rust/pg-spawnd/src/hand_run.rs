//! Explicit foreground and user-unit launch for hand-started train jobs.
use pg_spawnd::external_run::Registered;
use serde_json::json;
use std::{
    env, fs, io,
    os::unix::process::ExitStatusExt,
    path::{Path, PathBuf},
    process::{Command, ExitStatus},
};

const USAGE: &str = "Usage: pg-spawnd hand-run [--review | --unit NAME --result PATH [--review-output PATH]] -- PROGRAM [ARGS ...]";

fn writer_target(cwd: &Path) -> Result<PathBuf, String> {
    pg_worker_core::cargo_cache::require_writer_target(
        cwd,
        env::var_os("CARGO_TARGET_DIR").as_deref(),
    )
    .map_err(|error| error.to_string())
}

fn status_code(status: ExitStatus) -> i32 {
    status
        .code()
        .unwrap_or_else(|| 128 + status.signal().unwrap_or(0))
}

pub(crate) fn execute(args: &[String]) -> Result<i32, String> {
    let separator = args.iter().position(|arg| arg == "--").ok_or(USAGE)?;
    let program = args
        .get(separator + 1)
        .filter(|program| !program.is_empty())
        .ok_or(USAGE)?;
    let command = &args[separator + 1..];
    let cwd = env::current_dir().map_err(|error| error.to_string())?;
    match &args[..separator] {
        [] => {
            let target = writer_target(&cwd)?;
            let _lease = pg_worker_core::cargo_cache::writer_lease(&target)
                .map_err(|error| error.to_string())?;
            let mut child = Command::new(program);
            child.args(&command[1..]).env("CARGO_TARGET_DIR", target);
            run_registered(&mut child, "implement")
                .map(status_code)
                .map_err(|error| format!("hand-run {program}: {error}"))
        }
        [review] if review == "--review" => {
            run_registered(Command::new(program).args(&command[1..]), "review")
                .map(status_code)
                .map_err(|error| format!("hand-run {program}: {error}"))
        }
        [child, result, review] if child == "--unit-child" => run_unit_child(
            &cwd,
            Path::new(result),
            (!review.is_empty()).then(|| Path::new(review)),
            command,
        ),
        [unit_flag, unit, result_flag, result, rest @ ..]
            if unit_flag == "--unit" && result_flag == "--result" =>
        {
            let review = match rest {
                [] => None,
                [option, file] if option == "--review-output" && !file.is_empty() => {
                    Some(cwd.join(file))
                }
                _ => return Err(USAGE.into()),
            };
            if unit.is_empty() || result.is_empty() {
                return Err(USAGE.into());
            }
            start_unit(&cwd, unit, &cwd.join(result), review.as_deref(), command)
        }
        _ => Err(USAGE.into()),
    }
}

fn start_unit(
    cwd: &Path,
    unit: &str,
    result: &Path,
    review: Option<&Path>,
    command: &[String],
) -> Result<i32, String> {
    let target = review.is_none().then(|| writer_target(cwd)).transpose()?;
    let tools = crate::native_writer_tools::snapshot(false)?;
    let path = tools["tool_path"]
        .as_str()
        .ok_or("hand-run tool PATH missing")?;
    let program = if command[0] == "codex" {
        PathBuf::from(
            tools["resolved_tools"]["codex"]
                .as_str()
                .ok_or("tool_missing: codex")?,
        )
    } else {
        crate::native_writer_tools::resolve_on_path(path.as_ref(), cwd, &command[0])?
    };
    let systemd_run =
        crate::native_writer_tools::resolve_on_path(path.as_ref(), cwd, "systemd-run")?;
    let launcher = env::current_exe().map_err(|error| error.to_string())?;
    let mut unit_command = Command::new(systemd_run);
    unit_command
        .args(["--user", "--collect", "--property=Type=exec"])
        .arg(format!("--unit={unit}"))
        .arg(format!("--working-directory={}", cwd.display()))
        .arg(format!("--setenv=PATH={path}"));
    for key in [
        "PLANGRAPH_SPAWN_STATE",
        "XDG_STATE_HOME",
        "PLANGRAPH_HOST",
        "PLANGRAPH_SPAWN_ITEM",
    ] {
        if let Ok(value) = env::var(key) {
            unit_command.arg(format!("--setenv={key}={value}"));
        }
    }
    if let Some(target) = target {
        unit_command
            .arg(format!("--setenv=CARGO_TARGET_DIR={}", target.display()))
            .arg(format!(
                "--setenv=PLANGRAPH_CARGO_TARGET_DIR={}",
                target.display()
            ));
    }
    let status = unit_command
        .arg("--")
        .arg(launcher)
        .args(["hand-run", "--unit-child"])
        .arg(result)
        .arg(review.unwrap_or_else(|| Path::new("")))
        .arg("--")
        .arg(program)
        .args(&command[1..])
        .status()
        .map_err(|error| format!("hand-run {unit}: {error}"))?;
    Ok(status_code(status))
}

fn run_unit_child(
    cwd: &Path,
    result: &Path,
    review: Option<&Path>,
    command: &[String],
) -> Result<i32, String> {
    let mut child = Command::new(&command[0]);
    child.args(&command[1..]);
    let target = review.is_none().then(|| writer_target(cwd)).transpose()?;
    let _lease = target
        .as_deref()
        .map(pg_worker_core::cargo_cache::writer_lease)
        .transpose()
        .map_err(|error| error.to_string())?;
    if let Some(target) = target {
        child.env("CARGO_TARGET_DIR", target);
    }
    let started = chrono::Utc::now();
    let (rc, failure) = match run_registered(
        &mut child,
        if review.is_some() {
            "review"
        } else {
            "implement"
        },
    ) {
        Ok(status) => (status_code(status), None),
        Err(error) => {
            let rc = if error.kind() == io::ErrorKind::NotFound {
                127
            } else {
                126
            };
            (rc, Some(error.kind()))
        }
    };
    let review_status = review
        .map(|file| {
            if fs::read_to_string(file).is_ok_and(|text| !text.trim().is_empty()) {
                " verdict=output_present"
            } else {
                " verdict=no_verdict"
            }
        })
        .unwrap_or_default();
    let stuck = rc != 0 || review_status == " verdict=no_verdict";
    let mut row = format!("START {}\n", started.to_rfc3339());
    if stuck {
        let error = failure
            .map(|kind| format!(" spawn_error={kind}"))
            .unwrap_or_default();
        let alert = format!("ALERT stuck-work rc={rc}{review_status}{error}");
        eprintln!("{alert}");
        row.push_str(&format!("{alert}\n"));
    }
    row.push_str(&format!(
        "END {} rc={rc}{review_status}\n",
        chrono::Utc::now().to_rfc3339()
    ));
    pg_worker_core::run::record_io::atomic_write_private(result, row.as_bytes())
        .map_err(|error| format!("hand-run result: {error}"))?;
    Ok(if stuck && rc == 0 { 1 } else { rc })
}

fn run_registered(command: &mut Command, role: &str) -> io::Result<ExitStatus> {
    let state =
        crate::state::resolve_state(None, |key| env::var_os(key)).map_err(io::Error::other)?;
    let harness = Path::new(command.get_program())
        .file_name()
        .and_then(|name| name.to_str());
    let host = env::var("PLANGRAPH_HOST")
        .ok()
        .filter(|host| !host.is_empty())
        .unwrap_or_else(pg_spawnd::session::local_hostname);
    let mut lifecycle = Registered::start(
        state,
        json!({
            "run_kind":"external-harness", "host":host, "role":role, "harness":harness,
            "item_key":env::var("PLANGRAPH_SPAWN_ITEM").ok(),
        }),
    )?;
    let mut child = match command.spawn() {
        Ok(child) => child,
        Err(error) => {
            let code = if error.kind() == io::ErrorKind::NotFound {
                127
            } else {
                126
            };
            lifecycle.finish("failed", code, Some("launch_failed"))?;
            return Err(error);
        }
    };
    let registered = lifecycle.child(child.id());
    let exit = child.wait()?;
    registered?;
    let status = if exit.success() {
        "completed"
    } else if matches!(
        exit.signal(),
        Some(libc::SIGINT | libc::SIGTERM | libc::SIGHUP)
    ) {
        "cancelled"
    } else {
        "failed"
    };
    lifecycle.finish(status, status_code(exit), None)?;
    Ok(exit)
}
