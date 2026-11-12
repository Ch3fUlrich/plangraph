use std::io::{Read, Write};
use std::process::Command;

fn run() -> std::io::Result<i32> {
    let args: Vec<_> = std::env::args_os().collect();
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
    std::io::stdin().read_to_end(&mut payload)?;
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
fn main() {
    std::panic::set_hook(Box::new(|_| {})); // Shadow panics belong in JSONL, not Python stderr.
    let result = std::panic::catch_unwind(run)
        .unwrap_or_else(|_| Err(std::io::Error::other("hook harness panicked")));
    std::process::exit(result.unwrap_or_else(|_| {
        let _ = writeln!(std::io::stderr(), "pg-hook: Python hook could not run");
        0
    }));
}
