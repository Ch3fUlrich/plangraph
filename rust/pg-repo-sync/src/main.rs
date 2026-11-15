mod drift;
mod hooks;
mod refresh;
use drift::text;
use std::{
    env, fs,
    path::{Path, PathBuf},
    process::ExitCode,
};

fn sync(repo: &Path) -> Result<(), String> {
    if !text(repo, &["fetch", "-q", "--prune", "origin"]).is_ok() {
        return Err("fetch-failed".into());
    }
    let default = match text(
        repo,
        &["symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"],
    ) {
        Ok(value) => value
            .strip_prefix("refs/remotes/origin/")
            .map(str::to_owned)
            .ok_or("fetch-failed")?,
        Err(Some(1)) => "main".into(),
        Err(_) => return Err("fetch-failed".into()),
    };
    match text(repo, &["symbolic-ref", "--quiet", "--short", "HEAD"]) {
        Ok(branch) if branch != default => return Err(format!("other-branch:{branch}")),
        Err(_) => return Err("detached".into()),
        _ => {}
    }
    let flags = text(repo, &["ls-files", "-v", "-z"]).map_err(|_| "tracked-changes")?;
    let status = text(
        repo,
        &[
            "status",
            "--porcelain=v1",
            "--untracked-files=no",
            "--ignore-submodules=untracked",
        ],
    )
    .map_err(|_| "tracked-changes")?;
    if !status.is_empty()
        || flags.split('\0').any(|e| {
            e.as_bytes()
                .first()
                .is_some_and(|b| *b == b'S' || b.is_ascii_lowercase())
        })
    {
        return Err("tracked-changes".into());
    }
    let target = format!("refs/remotes/origin/{default}");
    let behind = text(repo, &["rev-list", "--count", &format!("HEAD..{target}")])
        .ok()
        .and_then(|s| s.parse::<u64>().ok())
        .ok_or("diverged")?;
    if behind == 0 {
        return Ok(());
    }
    if text(repo, &["merge-base", "--is-ancestor", "HEAD", &target]).is_err() {
        return Err("diverged".into());
    }
    if text(
        repo,
        &[
            "merge",
            "--ff-only",
            "--no-autostash",
            "--no-overwrite-ignore",
            &target,
        ],
    )
    .is_ok()
    {
        Ok(())
    } else {
        Err("diverged".into())
    }
}

fn run() -> Result<bool, &'static str> {
    let mut repos = Vec::new();
    let mut excludes = Vec::new();
    let mut roots = Vec::new();
    let mut policy = drift::Policy::default();
    let mut check_config = false;
    let mut also_sync = false;
    let (mut refresh_prs, mut notify) = (None, None);
    let mut args = env::args_os().skip(1);
    while let Some(arg) = args.next() {
        match arg.to_str() {
            Some("--repo") => repos.push(PathBuf::from(args.next().ok_or("missing repo")?)),
            Some("--root") => roots.push(PathBuf::from(args.next().ok_or("missing root")?)),
            Some("--exclude") => excludes.push(
                PathBuf::from(args.next().ok_or("missing exclude")?)
                    .canonicalize().map_err(|_| "invalid exclude")?),
            Some("--sync") => also_sync = true,
            Some("--refresh-prs") => refresh_prs = Some(args.next().ok_or("missing GitHub repository")?.to_string_lossy().into_owned()),
            Some("--notify") => notify = Some(PathBuf::from(args.next().ok_or("missing notifier")?)),
            Some("--check-config") => check_config = true,
            Some("--host") => policy.host = args.next().ok_or("missing host")?.to_string_lossy().into(),
            Some("--allow-name") => policy.names.push(args.next().ok_or("missing name")?.to_string_lossy().into()),
            Some("--allow-email") => policy.emails.push(args.next().ok_or("missing email")?.to_string_lossy().into()),
            _ => return Err("usage: pg-repo-sync --repo PATH --check-config --host ID --allow-name NAME --allow-email EMAIL"),
        }
    }
    if refresh_prs.is_some() != notify.is_some()
        || (refresh_prs.is_some()
            && (repos.len() != 1 || !roots.is_empty() || (check_config && !also_sync)))
    {
        return Err("--refresh-prs and --notify require one explicit --repo and sync enabled");
    }
    if check_config
        && (policy.host.is_empty() || policy.names.is_empty() || policy.emails.is_empty())
    {
        return Err("config check requires host and identity allow lists");
    }
    if roots.is_empty() && repos.is_empty() {
        if let Some(home) = env::var_os("HOME").filter(|s| !s.is_empty()) {
            roots.push(PathBuf::from(home).join("code"));
        }
    }
    for root in roots {
        match fs::read_dir(root) {
            Ok(entries) => {
                for entry in entries {
                    let path = entry.map_err(|_| "repository discovery failed")?.path();
                    if path.is_dir() && path.join(".git").exists() {
                        repos.push(path);
                    }
                }
            }
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
            Err(_) => return Err("repository discovery failed"),
        }
    }
    repos.sort();
    repos.dedup();
    if repos.is_empty() {
        return Ok(false);
    }
    let live = if !check_config || also_sync {
        drift::live_paths()
    } else {
        Ok(Vec::new())
    };
    hooks::discover(
        &mut repos,
        live.as_ref().ok().map(Vec::as_slice).unwrap_or(&[]),
        &excludes,
    )
    .map_err(|_| "hook checkout discovery failed")?;
    let mut alerts = false;
    for repo in repos {
        if fs::symlink_metadata(repo.join(".repo-sync-pin")).is_ok()
            || repo.canonicalize().is_ok_and(|p| excludes.contains(&p))
        {
            println!("SKIP {:?} excluded or pinned", repo.display().to_string());
            continue;
        }
        if !drift::is_checkout(&repo) {
            println!("ALERT {:?} reason=fetch-failed", repo.display().to_string());
            alerts = true;
            continue;
        }
        let differences = if check_config {
            drift::check(&repo, &policy)
        } else {
            Vec::new()
        };
        if !differences.is_empty() {
            println!(
                "ALERT host={} path={:?} {}",
                policy.host.escape_default(),
                repo.display().to_string(),
                differences.join(" ")
            );
            alerts = true;
            continue;
        }
        if !check_config || also_sync {
            let canonical = repo.canonicalize().map_err(|_| "invalid checkout")?;
            if live.as_ref().map_or(true, |paths| {
                paths.iter().any(|p| p.starts_with(&canonical))
            }) {
                println!(
                    "SKIP {:?} live-unit or unreadable unit protection",
                    repo.display().to_string()
                );
                alerts |= live.is_err();
                continue;
            }
            if let Err(reason) = sync(&repo) {
                if check_config {
                    println!(
                        "ALERT host={} path={:?} reason={}",
                        policy.host.escape_default(),
                        repo.display().to_string(),
                        reason.escape_debug()
                    );
                } else {
                    println!("ALERT {} reason={}", repo.display(), reason.escape_debug());
                }
                alerts = true;
            } else if let (Some(github), Some(notifier)) = (&refresh_prs, &notify) {
                match refresh::sweep(&repo, github, notifier) {
                    Ok(problems) => alerts |= problems,
                    Err(error) => {
                        eprintln!("PR refresh failed: {error}");
                        alerts = true;
                    }
                }
            }
        }
    }
    Ok(alerts)
}
fn main() -> ExitCode {
    if env::args_os().nth(1).as_deref() == Some(std::ffi::OsStr::new("refresh-prs")) {
        return match refresh::run(env::args_os().skip(2)) {
            Ok(problems) => u8::from(problems).into(),
            Err(error) => {
                eprintln!("pg-repo-sync: {error}");
                ExitCode::FAILURE
            }
        };
    }
    match run() {
        Ok(alerts) => u8::from(alerts).into(),
        Err(error) => {
            eprintln!("pg-repo-sync: {error}");
            ExitCode::FAILURE
        }
    }
}
