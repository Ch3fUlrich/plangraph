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

fn text(repo: &Path, args: &[&str]) -> Option<String> {
    let output = git(repo, args).stdout(Stdio::piped()).output().ok()?;
    if !output.status.success() {
        return None;
    }
    Some(String::from_utf8(output.stdout).ok()?.trim().to_owned())
}

fn fetch(repo: &Path) -> bool {
    let started = Instant::now();
    let Ok(mut child) = git(repo, &["fetch", "-q", "origin"]).spawn() else {
        return false;
    };
    loop {
        match child.try_wait() {
            Ok(Some(status)) => return status.success(),
            Ok(None) if started.elapsed() < Duration::from_secs(60) => {
                thread::sleep(Duration::from_millis(50));
            }
            _ => {
                let _ = child.kill();
                let _ = child.wait();
                return false;
            }
        }
    }
}

fn sync(repo: &Path) -> (u64, Option<String>) {
    let fetched = repo.join(".git").exists() && fetch(repo);
    let default = text(
        repo,
        &["symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"],
    )
    .and_then(|s| s.strip_prefix("refs/remotes/origin/").map(str::to_owned))
    .unwrap_or_else(|| "main".to_owned());
    let target = format!("refs/remotes/origin/{default}");
    let count = text(repo, &["rev-list", "--count", &format!("HEAD..{target}")])
        .and_then(|s| s.parse::<u64>().ok());
    let behind = count.unwrap_or(0);
    let reason = if !fetched || count.is_none() {
        "fetch-failed".to_owned()
    } else if behind == 0 {
        return (0, None);
    } else if let Some(branch) = text(repo, &["symbolic-ref", "--quiet", "--short", "HEAD"]) {
        if branch != default {
            format!("other-branch:{branch}")
        } else if text(
            repo,
            &[
                "status",
                "--porcelain=v1",
                "--untracked-files=no",
                "--ignore-submodules=untracked",
            ],
        )
        .is_none_or(|s| !s.is_empty())
        {
            "tracked-changes".to_owned()
        } else if !git(repo, &["merge-base", "--is-ancestor", "HEAD", &target])
            .status()
            .is_ok_and(|s| s.success())
            || !git(
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
            "diverged".to_owned()
        } else {
            return (behind, None);
        }
    } else {
        "detached".to_owned()
    };
    (behind, Some(reason))
}

fn run() -> Result<bool, &'static str> {
    let mut roots = Vec::new();
    let mut repos = Vec::new();
    let mut verbose = false;
    let mut args = env::args_os().skip(1);
    while let Some(arg) = args.next() {
        match arg.to_str() {
            Some("--root") => roots.push(PathBuf::from(args.next().ok_or("missing root")?)),
            Some("--repo") => repos.push(PathBuf::from(args.next().ok_or("missing repo")?)),
            Some("--verbose") => verbose = true,
            _ => return Err("usage: pg-repo-sync [--root DIR]... [--repo PATH]... [--verbose]"),
        }
    }
    if roots.is_empty() && repos.is_empty() {
        roots.push(PathBuf::from(env::var_os("HOME").ok_or("HOME is unavailable")?).join("code"));
    }
    for root in roots {
        for entry in fs::read_dir(root).map_err(|_| "repository discovery failed")? {
            let path = entry.map_err(|_| "repository discovery failed")?.path();
            let name = path.file_name().unwrap_or_default().to_string_lossy();
            if path.is_dir()
                && path.join(".git").exists()
                && !name.ends_with("-worktrees")
                && !name.ends_with("-lanes")
            {
                repos.push(path);
            }
        }
    }
    let mut repos: Vec<_> = repos
        .into_iter()
        .map(|p| p.canonicalize().unwrap_or(p))
        .collect();
    repos.sort();
    repos.dedup();
    let mut alerts = false;
    for repo in repos {
        let name: String = repo
            .file_name()
            .unwrap_or_default()
            .to_string_lossy()
            .chars()
            .flat_map(char::escape_default)
            .collect();
        let (behind, reason) = sync(&repo);
        if let Some(reason) = reason {
            println!("ALERT {name} behind={behind} reason={reason}");
            alerts = true;
        } else if verbose && behind == 0 {
            println!("OK {name}");
        }
    }
    Ok(alerts)
}

fn main() -> ExitCode {
    match run() {
        Ok(alerts) => ExitCode::from(u8::from(alerts)),
        Err(error) => {
            eprintln!("pg-repo-sync: {error}");
            ExitCode::from(1)
        }
    }
}
