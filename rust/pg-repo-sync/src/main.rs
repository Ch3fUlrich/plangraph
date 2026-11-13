use std::ffi::OsStr;
use std::fmt::Write;
use std::path::{Path, PathBuf};
use std::process::{Command, ExitCode, Stdio};
use std::{env, fs};

fn git(repo: &Path, args: &[&str]) -> Command {
    let mut command = Command::new("git");
    command
        .arg("-C")
        .arg(repo)
        .args([
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.fsmonitor=false",
        ])
        .args(args)
        .env("GIT_DIR", ".git")
        .env("GIT_TERMINAL_PROMPT", "0")
        .env("GIT_OPTIONAL_LOCKS", "0")
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    for key in ["GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"] {
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

fn sync(repo: &Path) -> Result<(), String> {
    let probe = |args: &[&str]| text(repo, args).map_err(|_| "fetch-failed".to_owned());
    if !git(repo, &["fetch", "-q", "origin"])
        .status()
        .is_ok_and(|s| s.success())
    {
        return Err("fetch-failed".to_owned());
    }
    let target = probe(&["symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"])?;
    let default = target
        .strip_prefix("refs/remotes/origin/")
        .filter(|name| !name.is_empty())
        .ok_or("fetch-failed")?;
    let branch = match text(repo, &["symbolic-ref", "--quiet", "--short", "HEAD"]) {
        Ok(branch) => branch,
        Err(Some(1)) => return Err("detached".to_owned()),
        Err(_) => return Err("fetch-failed".to_owned()),
    };
    if branch != default {
        return Err(format!("other-branch:{branch}"));
    }
    if !probe(&["status", "--porcelain=v1", "--untracked-files=no"])?.is_empty() {
        return Err("tracked-changes".to_owned());
    }
    let counts = probe(&[
        "rev-list",
        "--left-right",
        "--count",
        &format!("HEAD...{target}"),
    ])?;
    let mut counts = counts.split_whitespace().map(str::parse::<u64>);
    let (Some(Ok(ahead)), Some(Ok(behind)), None) = (counts.next(), counts.next(), counts.next())
    else {
        return Err("fetch-failed".to_owned());
    };
    if ahead > 0 && behind > 0 {
        return Err("diverged".to_owned());
    }
    if git(repo, &["merge", "--ff-only", "--no-autostash", &target])
        .status()
        .is_ok_and(|s| s.success())
    {
        Ok(())
    } else {
        Err("fetch-failed".to_owned())
    }
}

fn escaped_label(label: &OsStr) -> String {
    let mut escaped = String::new();
    for chunk in label.as_encoded_bytes().utf8_chunks() {
        for ch in chunk.valid().chars() {
            if ch.is_control()
                || matches!(ch, '\\' | '\u{2028}'..='\u{202e}' | '\u{2066}'..='\u{2069}')
            {
                escaped.extend(ch.escape_default());
            } else {
                escaped.push(ch);
            }
        }
        for byte in chunk.invalid() {
            write!(escaped, "\\x{byte:02x}").unwrap();
        }
    }
    escaped
}

fn run() -> Result<bool, ()> {
    let mut repos = Vec::new();
    let mut args = env::args_os().skip(1);
    while let Some(arg) = args.next() {
        if arg != "--repo" {
            return Err(());
        }
        let label = args.next().ok_or(())?;
        repos.push((label.clone(), PathBuf::from(label)));
    }
    if repos.is_empty() {
        if let Some(home) = env::var_os("HOME") {
            if let Ok(entries) = fs::read_dir(PathBuf::from(home).join("code")) {
                for entry in entries.flatten() {
                    let path = entry.path();
                    if path.is_dir() && fs::symlink_metadata(path.join(".git")).is_ok() {
                        repos.push((entry.file_name(), path));
                    }
                }
            }
        }
    }
    repos.sort_by(|a, b| a.0.cmp(&b.0));
    let mut alerts = false;
    for (label, repo) in repos {
        if let Err(reason) = sync(&repo) {
            println!("ALERT {} reason={reason}", escaped_label(&label));
            alerts = true;
        }
    }
    Ok(alerts)
}

fn main() -> ExitCode {
    match run() {
        Ok(alerts) => ExitCode::from(u8::from(alerts)),
        Err(()) => {
            eprintln!("Usage: pg-repo-sync [--repo PATH]...");
            ExitCode::from(2)
        }
    }
}
