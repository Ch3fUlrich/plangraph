use std::io::{Read, Write};
use std::path::Path;
use std::process::Command;

fn run(args: &[std::ffi::OsString]) -> std::io::Result<i32> {
    let Some((entry, log)) = fault_target(args) else {
        let _ = writeln!(
            std::io::stderr(),
            "usage: pg-hook shadow ENTRY LOG_PATH PYTHON_HOOK_PATH"
        );
        return Ok(0);
    };
    let mut payload = Vec::new();
    if std::io::stdin().read_to_end(&mut payload).is_err() {
        pg_hook::record_harness_fault(entry, log, "stdin read failed");
        return Err(std::io::Error::other("stdin read failed"));
    }
    let mut python = Command::new("python3");
    python.arg(&args[4]);
    let output = pg_hook::shadow(
        entry,
        &payload,
        &std::env::vars_os().collect(),
        &mut python,
        pg_hook::registry()[entry],
        log,
    )?;
    let _ = std::io::stdout().write_all(&output.stdout);
    let _ = std::io::stderr().write_all(&output.stderr);
    Ok(pg_hook::exit_code(&output))
}
fn finish(
    run: impl FnOnce() -> std::io::Result<i32> + std::panic::UnwindSafe,
    fault: Option<(&str, &Path)>,
    diagnostic: &mut dyn Write,
) -> i32 {
    let result = std::panic::catch_unwind(run).unwrap_or_else(|_| {
        if let Some((entry, log)) = fault {
            pg_hook::record_harness_fault(entry, log, "hook harness panicked");
        }
        Err(std::io::Error::other("hook harness panicked"))
    });
    result.unwrap_or_else(|error| {
        let _ = writeln!(diagnostic, "pg-hook: {error}");
        0
    })
}
fn main() {
    std::panic::set_hook(Box::new(|_| {})); // Shadow panics belong in JSONL, not Python stderr.
    let args: Vec<_> = std::env::args_os().collect();
    let fault = fault_target(&args);
    std::process::exit(finish(|| run(&args), fault, &mut std::io::stderr()));
}

fn fault_target(args: &[std::ffi::OsString]) -> Option<(&str, &Path)> {
    if args.len() != 5 || args[1] != "shadow" || args[3].is_empty() {
        return None;
    }
    let entry = args[2].to_str()?;
    pg_hook::registry()
        .contains_key(entry)
        .then_some((entry, Path::new(&args[3])))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn malformed_args_cannot_arm_a_panic_log() {
        let tmp = tempfile::tempdir().unwrap();
        for tokens in [
            vec!["pg-hook", "invalid", "bash_guard", "LOG", "hook.py"],
            vec!["pg-hook", "shadow", "unknown", "LOG", "hook.py"],
            vec!["pg-hook", "shadow", "bash_guard", "LOG"],
            vec!["pg-hook", "shadow", "bash_guard", "LOG", "hook.py", "extra"],
            vec!["pg-hook", "shadow", "bash_guard", "", "hook.py"],
            vec![],
        ] {
            let log = tmp.path().join("unvalidated.jsonl");
            let args: Vec<std::ffi::OsString> = tokens
                .into_iter()
                .map(|token| {
                    if token == "LOG" {
                        log.as_os_str().to_owned()
                    } else {
                        token.into()
                    }
                })
                .collect();
            let fault = fault_target(&args);
            let mut diagnostic = Vec::new();
            assert_eq!(
                finish(|| panic!("synthetic harness panic"), fault, &mut diagnostic),
                0
            );
            assert!(
                !log.exists(),
                "malformed arguments must not select a panic log"
            );
            assert!(fault.is_none());
        }
    }

    #[test]
    fn panicking_run_fails_open_and_records_harness_fault() {
        let tmp = tempfile::tempdir().unwrap();
        let log = tmp.path().join("panic.jsonl");
        let args = [
            "pg-hook".into(),
            "shadow".into(),
            "bash_guard".into(),
            log.as_os_str().to_owned(),
            "hook.py".into(),
        ];
        let fault = fault_target(&args);
        assert_eq!(fault, Some(("bash_guard", log.as_path())));
        let mut diagnostic = Vec::new();
        assert_eq!(
            finish(|| panic!("synthetic harness panic"), fault, &mut diagnostic),
            0
        );
        assert_eq!(diagnostic, b"pg-hook: hook harness panicked\n");
        let text = std::fs::read_to_string(log).unwrap();
        assert_eq!(text.lines().count(), 1);
        let record: serde_json::Value = serde_json::from_str(&text).unwrap();
        assert_eq!(record["entry"], "bash_guard");
        assert!(record["same"].is_null());
        assert_eq!(record["harness"]["error"], "hook harness panicked");
    }
}
