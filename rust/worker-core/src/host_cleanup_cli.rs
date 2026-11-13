//! Additional read-only host inventory and explicit merged sweep entry points.
use crate::prod_paths;
use std::{env, io, path::PathBuf, process::ExitCode};

pub fn prod_paths() -> ExitCode {
    match check_paths() {
        Ok(clean) if clean => ExitCode::SUCCESS,
        Ok(_) => ExitCode::FAILURE,
        Err(e) => {
            eprintln!("prod-paths: {e}");
            ExitCode::FAILURE
        }
    }
}

fn check_paths() -> io::Result<bool> {
    let home = PathBuf::from(env::var_os("HOME").ok_or_else(|| io::Error::other("HOME required"))?);
    let mut units = home.join(".config/systemd/user");
    let mut roots = prod_paths::default_roots(&home);
    let mut repos = vec![];
    let mut json = false;
    let mut args = env::args().skip(2);
    while let Some(flag) = args.next() {
        if flag == "--json" {
            json = true;
            continue;
        }
        let value = args
            .next()
            .ok_or_else(|| io::Error::other("missing option value"))?;
        let path = prod_paths::normalize(&PathBuf::from(value), &home);
        match flag.as_str() {
            "--unit-dir" => units = path,
            "--allow-root" => roots.push(path),
            "--repo" => repos.push(path),
            _ => return Err(io::Error::other("unknown prod-paths option")),
        }
    }
    if repos.is_empty() {
        repos = [
            home.join("code/plangraph"),
            home.join("code/plangraph-data"),
        ]
        .into_iter()
        .filter(|p| p.exists())
        .collect();
    }
    let refs = prod_paths::inventory(&units, &home, &repos)?;
    let issues = prod_paths::issues(&refs, &roots);
    if json {
        println!(
            "{}",
            serde_json::json!({"ok": issues.is_empty(), "issues": issues, "allowed_roots": roots})
        );
    } else {
        for issue in &issues {
            println!(
                "{} {}: {}",
                issue.kind,
                issue.file.display(),
                issue.path.display()
            );
        }
    }
    Ok(issues.is_empty())
}
