//! Checked-in CI settings and per-host settings use the same parser.
use std::{
    path::{Path, PathBuf},
    process::Command,
};
fn main() {
    if let Err(error) = execute() {
        eprintln!("{error}");
        std::process::exit(1);
    }
}
fn execute() -> Result<(), String> {
    let args: Vec<_> = std::env::args_os().skip(1).collect();
    let mut paths: Vec<PathBuf> = Vec::new();
    if args.first().is_some_and(|a| a == "--tracked") && args.len() == 2 {
        let root = Path::new(&args[1]);
        let output = Command::new("git")
            .arg("-C")
            .arg(root)
            .args(["ls-files", "-z", "--full-name"])
            .output()
            .map_err(|_| "settings lint: git failed")?;
        if !output.status.success() {
            return Err("settings lint: git failed".into());
        }
        for name in output.stdout.split(|b| *b == 0).filter(|s| !s.is_empty()) {
            let name = std::str::from_utf8(name).map_err(|_| "settings lint: invalid filename")?;
            let path = Path::new(name);
            if path
                .file_name()
                .is_some_and(|n| n == "settings.json" || n == "settings.local.json")
            {
                paths.push(root.join(path));
            }
        }
    } else if !args.is_empty() {
        paths.extend(args.iter().map(PathBuf::from));
    } else {
        return Err("usage: pg-settings-lint FILE... | --tracked REPO".into());
    }
    let mut errors = Vec::new();
    for path in paths {
        let text = match std::fs::read_to_string(&path) {
            Ok(text) => text,
            Err(e) => {
                errors.push(format!("{}:1: {e}", path.display()));
                continue;
            }
        };
        if let Err(error) = pg_hook::settings_lint::check(&text) {
            errors.push(format!(
                "{}:{}: invalid settings permission rule",
                path.display(),
                error.line()
            ));
        }
    }
    if errors.is_empty() {
        Ok(())
    } else {
        Err(errors.join("\n"))
    }
}
