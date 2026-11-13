mod drift;
use drift::{git, text};
use std::{
    env, fs,
    path::{Path, PathBuf},
    process::ExitCode,
    time::{Duration, Instant},
};

// Reused from the closed first slice, with its off-contract reason codes removed.
fn fetch(repo: &Path) -> bool {
    let mut cmd = git(repo, &["fetch", "-q", "origin"]);
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        cmd.process_group(0);
    }
    let Ok(mut child) = cmd.spawn() else {
        return false;
    };
    let start = Instant::now();
    loop {
        match child.try_wait() {
            Ok(Some(status)) => return status.success(),
            Ok(None) if start.elapsed() < Duration::from_secs(60) => {
                std::thread::sleep(Duration::from_millis(50))
            }
            _ => {
                #[cfg(unix)]
                // SAFETY: the owned child leads its own process group.
                unsafe {
                    unsafe extern "C" {
                        fn kill(pid: i32, signal: i32) -> i32;
                    }
                    kill(-(child.id() as i32), 9);
                }
                let _ = child.kill();
                let _ = child.wait();
                return false;
            }
        }
    }
}
fn sync(repo: &Path) -> Result<(), String> {
    if !fetch(repo) {
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
    if !git(repo, &["merge-base", "--is-ancestor", "HEAD", &target])
        .status()
        .is_ok_and(|s| s.success())
    {
        return Err("diverged".into());
    }
    if git(
        repo,
        &[
            "merge",
            "--ff-only",
            "--no-autostash",
            "--no-overwrite-ignore",
            &target,
        ],
    )
    .status()
    .is_ok_and(|s| s.success())
    {
        Ok(())
    } else {
        Err("diverged".into())
    }
}

fn run() -> Result<bool, &'static str> {
    let mut repos = Vec::new();
    let mut roots = Vec::new();
    let mut policy = drift::Policy::default();
    let mut check_config = false;
    let mut also_sync = false;
    let mut args = env::args_os().skip(1);
    while let Some(arg) = args.next() {
        match arg.to_str() {
            Some("--repo") => repos.push(PathBuf::from(args.next().ok_or("missing repo")?)),
            Some("--root") => roots.push(PathBuf::from(args.next().ok_or("missing root")?)),
            Some("--sync") => also_sync = true,
            Some("--check-config") => check_config = true,
            Some("--host") => policy.host = args.next().ok_or("missing host")?.to_string_lossy().into(),
            Some("--allow-name") => policy.names.push(args.next().ok_or("missing name")?.to_string_lossy().into()),
            Some("--allow-email") => policy.emails.push(args.next().ok_or("missing email")?.to_string_lossy().into()),
            _ => return Err("usage: pg-repo-sync --repo PATH --check-config --host ID --allow-name NAME --allow-email EMAIL"),
        }
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
    let mut alerts = false;
    for repo in repos {
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
            }
        }
    }
    Ok(alerts)
}
fn main() -> ExitCode {
    match run() {
        Ok(alerts) => u8::from(alerts).into(),
        Err(error) => {
            eprintln!("pg-repo-sync: {error}");
            ExitCode::FAILURE
        }
    }
}
