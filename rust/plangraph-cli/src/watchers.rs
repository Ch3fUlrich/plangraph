//! Session-independent watcher services and durable, non-consuming recovery records.
use crate::atomic;
use serde::{Deserialize, Serialize};
use std::collections::hash_map::DefaultHasher;
use std::fs;
use std::hash::{Hash, Hasher};
use std::io;
use std::path::Path;
use std::process::{Command, Stdio};

#[derive(Serialize, Deserialize)]
struct Request {
    argv: Vec<String>,
    cwd: String,
    #[serde(default)]
    unit: String,
}

#[derive(Serialize, Deserialize)]
struct Outcome {
    code: i32,
    error: Option<String>,
}

fn read<T: serde::de::DeserializeOwned>(path: &Path) -> io::Result<T> {
    toml::from_str(&fs::read_to_string(path)?).map_err(io::Error::other)
}

fn write<T: Serialize>(path: &Path, value: &T) -> io::Result<()> {
    atomic::write(
        path,
        toml::to_string(value).map_err(io::Error::other)?.as_bytes(),
    )
}

/// Variables the user manager sets itself for a service; copying them would misname the unit.
const MANAGER_VARS: [&str; 12] = [
    "INVOCATION_ID",
    "JOURNAL_STREAM",
    "SYSTEMD_EXEC_PID",
    "NOTIFY_SOCKET",
    "LISTEN_FDS",
    "LISTEN_PID",
    "LISTEN_FDNAMES",
    "MAINPID",
    "MANAGERPID",
    "WATCHDOG_PID",
    "WATCHDOG_USEC",
    "FDSTORE",
];

/// Names of the caller's environment a detached unit inherits: the whole login environment
/// (PATH with ~/.local/bin, HOME, XDG and opencode config vars), minus what the manager sets.
fn passthrough_names(vars: impl Iterator<Item = std::ffi::OsString>) -> Vec<String> {
    let mut names: Vec<String> = vars
        .filter_map(|name| name.into_string().ok())
        .filter(|name| {
            !name.is_empty()
                && !name.contains('=')
                && !name.bytes().any(|b| b == 0)
                && !MANAGER_VARS.contains(&name.as_str())
        })
        .collect();
    names.sort();
    names
}

fn service(exe: &Path, dir: &Path, unit: &str, env_names: &[String]) -> Command {
    let mut cmd = Command::new("systemd-run");
    // A transient service belongs to the user manager, not the caller's scope.
    // Type=exec makes admission wait for the runner's exec, not its completion.
    cmd.args([
        "--user",
        "--quiet",
        "--collect",
        "--service-type=exec",
        "--unit",
    ])
    .arg(unit);
    // `-E NAME` takes the value from systemd-run's own environment: no value on a command line.
    for name in env_names {
        cmd.arg("-E").arg(name);
    }
    cmd.arg("--")
        .arg(exe)
        .arg("watcher-run")
        .arg(dir)
        .stdin(Stdio::null());
    cmd
}

pub fn launch(job: &Path, name: &str, argv: &[String]) -> io::Result<()> {
    if name.is_empty()
        || !name
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')
        || argv.is_empty()
    {
        return Err(io::Error::other(
            "watcher needs a simple name and command argv",
        ));
    }
    fs::create_dir_all(job.join("watchers"))?;
    let dir = fs::canonicalize(job.join("watchers"))?.join(name);
    // Refuse duplicate submissions, including incomplete or failed admission.
    fs::create_dir(&dir)?;
    let mut hash = DefaultHasher::new();
    dir.hash(&mut hash);
    let unit = format!("pg-watcher-{:016x}", hash.finish());
    let request = Request {
        argv: argv.to_vec(),
        cwd: std::env::current_dir()?.to_string_lossy().into_owned(),
        unit,
    };
    write(&dir.join("request.toml"), &request)?;
    let names = passthrough_names(std::env::vars_os().map(|(name, _)| name));
    let admitted = service(&std::env::current_exe()?, &dir, &request.unit, &names).status();
    let error = match admitted {
        Ok(status) if status.success() => return Ok(()),
        Ok(status) => format!("systemd-run: {status}"),
        Err(error) => error.to_string(),
    };
    // Admission can be ambiguous; do not overwrite a service's eventual result
    // or automatically resubmit a command with side effects.
    atomic::write(&dir.join("launch-error.txt"), error.as_bytes())?;
    Err(io::Error::other(error))
}

pub fn run(dir: &Path) -> io::Result<i32> {
    let result = dir.join("result.toml");
    let _lock = atomic::lock(&result)?;
    if result.exists() {
        return Err(io::Error::other(
            "watcher already has a result; refusing replay",
        ));
    }
    let execute = || -> io::Result<i32> {
        let request: Request = read(&dir.join("request.toml"))?;
        let (program, args) = request
            .argv
            .split_first()
            .ok_or_else(|| io::Error::other("empty watcher command"))?;
        let stdout = atomic::private_options().open(dir.join("stdout.log"))?;
        let stderr = atomic::private_options().open(dir.join("stderr.log"))?;
        let status = Command::new(program)
            .args(args)
            .current_dir(&request.cwd)
            .stdin(Stdio::null())
            .stdout(stdout)
            .stderr(stderr)
            .status()?;
        #[cfg(unix)]
        {
            use std::os::unix::process::ExitStatusExt;
            Ok(status
                .code()
                .unwrap_or_else(|| 128 + status.signal().unwrap_or(0)))
        }
        #[cfg(not(unix))]
        Ok(status.code().unwrap_or(1))
    };
    let outcome = match execute() {
        Ok(code) => Outcome { code, error: None },
        Err(error) => Outcome {
            code: 1,
            error: Some(error.to_string()),
        },
    };
    write(&result, &outcome)?;
    Ok(outcome.code)
}

pub fn digest(job: &Path) -> io::Result<Vec<String>> {
    let root = job.join("watchers");
    let entries = match fs::read_dir(root) {
        Ok(entries) => entries,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(Vec::new()),
        Err(error) => return Err(error),
    };
    let mut rows = Vec::new();
    for entry in entries {
        let entry = entry?;
        if !entry.file_type()?.is_dir() {
            continue;
        }
        let dir = entry.path();
        let name = entry.file_name().to_string_lossy().into_owned();
        let result = dir.join("result.toml");
        let state = (|| -> io::Result<String> {
            Ok(if result.exists() {
                let outcome: Outcome = read(&result)?;
                format!(
                    "exit {}{}",
                    outcome.code,
                    outcome
                        .error
                        .map(|e| format!(" error: {e}"))
                        .unwrap_or_default()
                )
            } else {
                let request: Request = read(&dir.join("request.toml"))?;
                let error = match fs::read_to_string(dir.join("launch-error.txt")) {
                    Ok(error) => format!(" admission error: {error}"),
                    Err(error) if error.kind() == io::ErrorKind::NotFound => String::new(),
                    Err(error) => return Err(error),
                };
                format!("pending unit={}{}", request.unit, error)
            })
        })()
        .unwrap_or_else(|error| format!("record error: {error}"));
        rows.push(format!("{name}: {state} ({})", dir.display()));
    }
    rows.sort();
    Ok(rows)
}

pub fn cli(args: &[String]) -> Option<i32> {
    let command = args.first()?.as_str();
    if !matches!(command, "watcher-launch" | "watcher-run" | "resume-digest") {
        return None;
    }
    let result = match args
        .iter()
        .map(String::as_str)
        .collect::<Vec<_>>()
        .as_slice()
    {
        ["watcher-launch", job, name, "--", ..] if args.len() > 4 => {
            launch(Path::new(job), name, &args[4..]).map(|()| 0)
        }
        ["watcher-run", dir] => run(Path::new(dir)),
        ["resume-digest", job] => digest(Path::new(job)).map(|rows| {
            for row in rows {
                println!("{row}");
            }
            0
        }),
        _ => Err(io::Error::other(
            "usage: plangraph watcher-launch JOB NAME -- COMMAND [ARG...] | resume-digest JOB",
        )),
    };
    Some(result.unwrap_or_else(|error| {
        eprintln!("{error}");
        2
    }))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn watcher_service_is_detached_and_executes_durable_runner() {
        let command = service(
            Path::new("/bin/plangraph"),
            Path::new("/job/watchers/push"),
            "pg-watcher-test",
            &[],
        );
        assert_eq!(command.get_program(), "systemd-run");
        assert_eq!(
            command.get_args().collect::<Vec<_>>(),
            [
                "--user",
                "--quiet",
                "--collect",
                "--service-type=exec",
                "--unit",
                "pg-watcher-test",
                "--",
                "/bin/plangraph",
                "watcher-run",
                "/job/watchers/push",
            ]
        );
    }

    #[test]
    fn watcher_service_passes_the_login_environment_by_name() {
        let names = passthrough_names(
            [
                "PATH",
                "HOME",
                "XDG_CONFIG_HOME",
                "INVOCATION_ID",
                "JOURNAL_STREAM",
                "A=B",
            ]
            .into_iter()
            .map(std::ffi::OsString::from),
        );
        assert_eq!(names, ["HOME", "PATH", "XDG_CONFIG_HOME"]);
        let command = service(
            Path::new("/bin/plangraph"),
            Path::new("/job/watchers/push"),
            "pg-watcher-test",
            &names,
        );
        let args = command.get_args().collect::<Vec<_>>();
        let unit = args.iter().position(|a| *a == "pg-watcher-test").unwrap();
        let separator = args.iter().position(|a| *a == "--").unwrap();
        assert_eq!(
            &args[unit + 1..separator],
            ["-E", "HOME", "-E", "PATH", "-E", "XDG_CONFIG_HOME"]
        );
    }
}
