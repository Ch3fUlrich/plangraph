//! The one `cargo test` entry point of the native Windows CI job and of the pre-push Windows run: the blocking run
//! leaves out the active quarantined tests of the package, the reporting run (`--quarantine-only`) runs only them.
//! Without a ledger it is a plain `cargo test --no-fail-fast -p <package>`, which is what the pre-push run uses.
use std::io::{Read, Write};
use std::path::Path;
use std::process::{Command, Stdio};
use std::sync::{Arc, Mutex};

/// The cargo arguments for one package. `names` are the active quarantined test paths of that package.
pub fn cargo_args(package: &str, names: &[String], quarantine_only: bool) -> Vec<String> {
    let mut args: Vec<String> = ["test", "--no-fail-fast", "-p", package]
        .map(String::from)
        .to_vec();
    if quarantine_only {
        args.extend(["--".into(), "--exact".into()]);
        args.extend(names.iter().cloned());
    } else if !names.is_empty() {
        args.extend(["--".into(), "--exact".into()]);
        args.extend(names.iter().flat_map(|n| ["--skip".to_string(), n.clone()]));
    }
    args
}

fn pump(
    mut from: impl Read + Send + 'static,
    mut to: impl Write + Send + 'static,
    log: Option<Arc<Mutex<std::fs::File>>>,
) -> std::thread::JoinHandle<()> {
    std::thread::spawn(move || {
        let mut buffer = [0u8; 8192];
        while let Ok(n) = from.read(&mut buffer) {
            if n == 0 {
                break;
            }
            let _ = to.write_all(&buffer[..n]);
            if let Some(log) = &log {
                let _ = log.lock().unwrap().write_all(&buffer[..n]);
            }
        }
    })
}

/// Run cargo with the arguments, copying its output to ours and to `log`; the exit status is cargo's.
pub fn run(args: &[String], log: Option<&Path>) -> Result<i32, String> {
    let log = match log {
        Some(path) => Some(Arc::new(Mutex::new(
            std::fs::File::create(path).map_err(|e| format!("{}: {e}", path.display()))?,
        ))),
        None => None,
    };
    let mut child = Command::new("cargo")
        .args(args)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|e| format!("cargo: {e}"))?;
    let out = pump(child.stdout.take().unwrap(), std::io::stdout(), log.clone());
    let err = pump(child.stderr.take().unwrap(), std::io::stderr(), log);
    let status = child.wait().map_err(|e| e.to_string())?;
    let _ = (out.join(), err.join());
    Ok(status.code().unwrap_or(1))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_plain_run_has_no_filter_and_an_exclusion_names_each_test_exactly() {
        assert_eq!(
            cargo_args("pg-spawnd", &[], false),
            ["test", "--no-fail-fast", "-p", "pg-spawnd"]
        );
        let names = vec!["a::b".to_string(), "c::d".to_string()];
        assert_eq!(
            cargo_args("pg-spawnd", &names, false),
            [
                "test",
                "--no-fail-fast",
                "-p",
                "pg-spawnd",
                "--",
                "--exact",
                "--skip",
                "a::b",
                "--skip",
                "c::d"
            ]
        );
    }

    #[test]
    fn the_reporting_run_names_only_the_quarantined_tests() {
        let names = vec!["a::b".to_string()];
        assert_eq!(
            cargo_args("pg-worker-core", &names, true),
            [
                "test",
                "--no-fail-fast",
                "-p",
                "pg-worker-core",
                "--",
                "--exact",
                "a::b"
            ]
        );
    }
}
