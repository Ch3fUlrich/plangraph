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

fn sync(repo: &Path) -> (u64, Option<String>) {
    let fetched = repo.join(".git").exists() && fetch(repo);
    let default = match text(
        repo,
        &["symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"],
    ) {
        Ok(s) => s.strip_prefix("refs/remotes/origin/").map(str::to_owned),
        Err(Some(1)) => Some("main".to_owned()),
        _ => None,
    };
    let name = default.as_deref().unwrap_or("main");
    let target = format!("refs/remotes/origin/{name}");
    let count = text(repo, &["rev-list", "--count", &format!("HEAD..{target}")])
        .ok()
        .and_then(|s| s.parse::<u64>().ok());
    let behind = count.unwrap_or(0);
    let branch = text(repo, &["symbolic-ref", "--quiet", "--short", "HEAD"]);
    let reason = if !fetched {
        "fetch-failed".to_owned()
    } else if default.is_none() || count.is_none() {
        "probe-failed".to_owned()
    } else if behind == 0 {
        return (0, None);
    } else if let Ok(branch) = branch.as_ref() {
        if branch != name {
            format!("other-branch:{branch}")
        } else if text(repo, &["ls-files", "-v", "-z"]).ok().is_none_or(|s| {
            s.split('\0').any(|entry| {
                entry
                    .as_bytes()
                    .first()
                    .is_some_and(|tag| *tag == b'S' || tag.is_ascii_lowercase())
            })
        }) || text(
            repo,
            &[
                "status",
                "--porcelain=v1",
                "--untracked-files=no",
                "--ignore-submodules=untracked",
            ],
        )
        .ok()
        .is_none_or(|s| !s.is_empty())
        {
            "tracked-changes".to_owned()
        } else {
            match git(repo, &["merge-base", "--is-ancestor", "HEAD", &target])
                .status()
                .ok()
                .and_then(|s| s.code())
            {
                Some(1) => "diverged".to_owned(),
                Some(0) => {
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
                        return (behind, None);
                    } else {
                        "ff-failed".to_owned()
                    }
                }
                _ => "ancestry-check-failed".to_owned(),
            }
        }
    } else if branch == Err(Some(1)) {
        "detached".to_owned()
    } else {
        "branch-check-failed".to_owned()
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
            if fs::symlink_metadata(&path).is_ok_and(|metadata| metadata.is_dir())
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
