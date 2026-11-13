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

pub fn sweep_merged() -> ExitCode {
    match merged() {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("sweep-merged: {e}");
            ExitCode::FAILURE
        }
    }
}

fn merged() -> io::Result<()> {
    let home = PathBuf::from(env::var_os("HOME").ok_or_else(|| io::Error::other("HOME required"))?);
    let mut opts = crate::sweep_merged::Options {
        repo: PathBuf::new(),
        root: PathBuf::new(),
        units: home.join(".config/systemd/user"),
        state: home.join(".local/state/plangraph"),
        home,
        apply: false,
        remote: false,
        max: 20,
    };
    let mut json = false;
    let mut args = env::args().skip(2);
    while let Some(flag) = args.next() {
        match flag.as_str() {
            "--apply" => {
                opts.apply = true;
                continue;
            }
            "--dry-run" => {
                opts.apply = false;
                continue;
            }
            "--remote" => {
                opts.remote = true;
                continue;
            }
            "--json" => {
                json = true;
                continue;
            }
            _ => {}
        }
        let value = args
            .next()
            .ok_or_else(|| io::Error::other("missing option value"))?;
        if flag == "--max" {
            opts.max = value.parse().map_err(io::Error::other)?;
            continue;
        }
        let path = prod_paths::normalize(&PathBuf::from(value), &opts.home);
        match flag.as_str() {
            "--repo" => opts.repo = path,
            "--root" => opts.root = path,
            "--unit-dir" => opts.units = path,
            "--state" => opts.state = path,
            _ => return Err(io::Error::other("unknown sweep-merged option")),
        }
    }
    if opts.repo.as_os_str().is_empty() || opts.root.as_os_str().is_empty() {
        return Err(io::Error::other("--repo and --root required"));
    }
    let report = crate::sweep_merged::run(&opts)?;
    if json {
        println!("{}", serde_json::to_string(&report)?);
    } else {
        let verb = if report.dry_run {
            "would-remove"
        } else {
            "removed"
        };
        for action in report.actions {
            for tree in action.worktrees {
                println!("{verb} worktree {}", tree.display());
            }
            if action.delete_branch {
                println!("{verb} local branch {} {}", action.branch, action.sha);
            }
            if action.remote {
                println!("{verb} remote branch origin/{}", action.branch);
            }
        }
        for skip in report.skipped {
            println!("skip {skip}");
        }
        if let Some(path) = report.restore {
            println!("restore {}", path.display());
        }
    }
    Ok(())
}
