use std::io::{Read, Write};
use std::path::Path;
use std::process::Command;

fn run(args: &[std::ffi::OsString]) -> std::io::Result<i32> {
    let hooks = pg_hook::registry();
    if args.len() != 5 || args[1] != "shadow" || !hooks.contains_key(args[2].to_str().unwrap_or(""))
    {
        let _ = writeln!(
            std::io::stderr(),
            "usage: pg-hook shadow ENTRY LOG_PATH PYTHON_HOOK_PATH"
        );
        return Ok(0);
    }
    let entry = args[2].to_str().unwrap();
    let mut payload = Vec::new();
    if std::io::stdin().read_to_end(&mut payload).is_err() {
        pg_hook::record_harness_fault(entry, args[3].as_ref(), "stdin read failed");
        return Err(std::io::Error::other("stdin read failed"));
    }
    let mut python = Command::new("python3");
    python.arg(&args[4]);
    let output = pg_hook::shadow(
        entry,
        &payload,
        &std::env::vars_os().collect(),
        &mut python,
        hooks[entry],
        args[3].as_ref(),
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
    let fault = args
        .get(2)
        .and_then(|entry| entry.to_str())
        .zip(args.get(3).map(Path::new));
    std::process::exit(finish(|| run(&args), fault, &mut std::io::stderr()));
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn panicking_run_fails_open_and_records_harness_fault() {
        let tmp = tempfile::tempdir().unwrap();
        let log = tmp.path().join("panic.jsonl");
        let mut diagnostic = Vec::new();
        assert_eq!(
            finish(
                || panic!("synthetic harness panic"),
                Some(("bash_guard", &log)),
                &mut diagnostic
            ),
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
