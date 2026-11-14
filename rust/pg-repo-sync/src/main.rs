#[cfg(unix)]
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Command, ExitCode, Stdio};
use std::time::{Duration, Instant};
use std::{env, fs, thread};

fn git(repo: &Path, args: &[&str]) -> Command {
    let mut command = Command::new("git");
    command
        .arg("-C")
        .arg(repo)
        .args(["-c", "core.hooksPath=/dev/null"])
        .args(args)
        .env("GIT_TERMINAL_PROMPT", "0")
        .env("GIT_OPTIONAL_LOCKS", "0")
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    for key in [
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_COMMON_DIR",
    ] {
        command.env_remove(key);
    }
    command
}

fn text(repo: &Path, args: &[&str]) -> Result<String, Option<i32>> {
    let output = git(repo, args)
        .stdout(Stdio::piped())
        .output()
        .map_err(|_| None)?;
    if !output.status.success() {
        return Err(output.status.code());
    }
    String::from_utf8(output.stdout)
        .map(|s| s.trim().to_owned())
        .map_err(|_| None)
}

fn fetch(repo: &Path) -> bool {
    let started = Instant::now();
    let mut command = git(repo, &["fetch", "-q", "origin"]);
    #[cfg(unix)]
    command.process_group(0);
    let Ok(mut child) = command.spawn() else {
        return false;
    };
    loop {
        match child.try_wait() {
            Ok(Some(status)) => return status.success(),
            Ok(None) if started.elapsed() < Duration::from_secs(60) => {
                thread::sleep(Duration::from_millis(50));
            }
            _ => {
                #[cfg(unix)]
                unsafe {
                    unsafe extern "C" {
                        fn kill(pid: i32, signal: i32) -> i32;
                    }
                    // The unreaped child leads its own group; include remote helpers.
                    kill(-(child.id() as i32), 9); // SIGKILL
                }
                #[cfg(not(unix))]
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
    let mut args = env::args_os().skip(1);
    while let Some(arg) = args.next() {
        match arg.to_str() {
            Some("--repo") => repos.push(PathBuf::from(args.next().ok_or("missing repo")?)),
            Some("--root") => roots.push(PathBuf::from(args.next().ok_or("missing root")?)),
            _ => return Err("usage: pg-repo-sync [--root DIR] [--repo PATH]"),
        }
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
        if let Err(reason) = sync(&repo) {
            println!("ALERT {} reason={}", repo.display(), reason.escape_debug());
            alerts = true;
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
