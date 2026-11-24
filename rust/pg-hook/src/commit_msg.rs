//! Git entry point. Trailer block parsing and value policy live in ci_commit_gate.py.
use std::io::Write;
use std::path::Path;
use std::process::{Command, ExitCode, Stdio};

fn install(root: &Path) -> Result<i32, Box<dyn std::error::Error>> {
    let root = root.canonicalize()?;
    let hooks = root.join("tools/hooks");
    if !root.join("tools/ci_commit_gate.py").is_file() {
        return Err("checkout has no commit gate".into());
    }
    std::fs::copy(
        root.join("tools/ci_commit_gate.py"),
        hooks.join(".commit-msg-gate"),
    )?;
    let executable = std::env::current_exe()?;
    let destination = hooks.join("commit-msg");
    if destination.canonicalize().ok().as_ref() != Some(&executable) {
        std::fs::copy(&executable, &destination)?;
    }
    std::fs::set_permissions(&destination, std::fs::metadata(&executable)?.permissions())?;
    for args in [
        vec!["config", "extensions.worktreeConfig", "true"],
        vec!["config", "--worktree", "core.hooksPath", "tools/hooks"],
    ] {
        if !Command::new("git")
            .arg("-C")
            .arg(&root)
            .args(args)
            .status()?
            .success()
        {
            return Err("cannot configure worktree Git hooks".into());
        }
    }
    println!("Git hooks installed: {}", hooks.display());
    Ok(0)
}

fn config_value(key: &str) -> Result<Option<String>, Box<dyn std::error::Error>> {
    let output = Command::new("git")
        .args(["config", "--get", key])
        .output()?;
    match output.status.code() {
        Some(0) => Ok(Some(
            String::from_utf8(output.stdout)?.trim_end().to_owned(),
        )),
        Some(1) => Ok(None),
        _ => Err(format!("cannot read {key}").into()),
    }
}

const SCISSORS: &[u8] = b" ------------------------ >8 ------------------------";

/// Whether Git strips comments and cuts at the scissors line of this commit.
/// Git sets GIT_EDITOR=: when no editor is used (-m/-F/--no-edit); its default
/// cleanup then preserves comments, unlike editor commits.
fn strips_comments() -> Result<bool, Box<dyn std::error::Error>> {
    let editor = std::env::var_os("GIT_EDITOR").as_deref() != Some(std::ffi::OsStr::new(":"));
    Ok(match config_value("commit.cleanup")?.as_deref() {
        Some("strip" | "scissors") => true,
        Some("whitespace" | "verbatim") => false,
        _ => editor,
    })
}

/// The first scissors line Git will cut at, as (offset, comment prefix): the line is the comment
/// string followed by the fixed scissors text. A one-character comment character is the only
/// prefix; `auto` (or a string older Git ignores) lets Git pick one character from its list.
/// A literal look-alike is kept by Git.
fn scissors_marker<'a>(
    raw: &'a [u8],
    strip_comments: bool,
    configured: Option<&str>,
) -> Option<(usize, &'a [u8])> {
    let prefix_ok = |prefix: &[u8]| match configured {
        None => prefix == b"#",
        // Git before 2.45 ignores a multi-character core.commentString and picks a character itself.
        Some(configured) if configured == "auto" || configured.len() > 1 => {
            prefix == configured.as_bytes()
                || (prefix.len() == 1 && b"#;@!$%^&|:".contains(&prefix[0]))
        }
        Some(explicit) => prefix == explicit.as_bytes(),
    };
    raw.split_inclusive(|b| *b == b'\n')
        .scan(0, |offset, line| {
            let start = *offset;
            *offset += line.len();
            Some((start, line))
        })
        .find_map(|(start, line)| {
            let line = line.strip_suffix(b"\n").unwrap_or(line);
            let prefix = line.strip_suffix(SCISSORS)?;
            (strip_comments && prefix_ok(prefix)).then_some((start, prefix))
        })
}

/// The `--verbose` and `--cleanup` settings of the `git commit` that runs this hook, from its own
/// command line (`None` when the parent is not a readable `git commit`).
struct CommitFlags {
    verbose: Option<bool>,
    cleanup: Option<String>,
}

fn commit_flags() -> Option<CommitFlags> {
    #[cfg(unix)]
    {
        let parent = std::fs::read(format!(
            "/proc/{}/cmdline",
            std::os::unix::process::parent_id()
        ))
        .ok()?;
        let argv: Vec<String> = parent
            .split(|b| *b == 0)
            .filter(|arg| !arg.is_empty())
            .map(|arg| String::from_utf8_lossy(arg).into_owned())
            .collect();
        if !argv
            .first()
            .is_some_and(|git| git.rsplit('/').next() == Some("git"))
        {
            return None;
        }
        let mut args = argv[argv.iter().position(|arg| arg == "commit")? + 1..].iter();
        let mut flags = CommitFlags {
            verbose: None,
            cleanup: None,
        };
        while let Some(arg) = args.next() {
            if arg == "--" {
                break;
            }
            if let Some(long) = arg.strip_prefix("--") {
                match long.split_once('=') {
                    _ if long == "verbose" => flags.verbose = Some(true),
                    _ if long == "no-verbose" => flags.verbose = Some(false),
                    Some(("cleanup", mode)) => flags.cleanup = Some(mode.to_owned()),
                    None if long == "cleanup" => flags.cleanup = args.next().cloned(),
                    None if [
                        "message",
                        "file",
                        "reuse-message",
                        "reedit-message",
                        "template",
                        "author",
                        "date",
                        "fixup",
                        "squash",
                        "trailer",
                        "pathspec-from-file",
                    ]
                    .contains(&long) =>
                    {
                        args.next();
                    }
                    _ => {}
                }
            } else if let Some(cluster) = arg.strip_prefix('-').filter(|c| !c.is_empty()) {
                for (index, flag) in cluster.char_indices() {
                    match flag {
                        'v' => flags.verbose = Some(true),
                        'm' | 'F' | 'C' | 'c' | 't' => {
                            if index + 1 == cluster.len() {
                                args.next();
                            }
                            break;
                        }
                        'S' | 'u' => break,
                        _ => {}
                    }
                }
            }
        }
        Some(flags)
    }
    #[cfg(not(unix))]
    {
        None
    }
}

/// Whether Git cuts the message at its scissors line: with `--verbose` or `commit.verbose`, or for an
/// editor message under `--cleanup=scissors` / `commit.cleanup=scissors` (without an editor that
/// mode falls back to whitespace cleanup). `None` when the command line is unknown.
fn git_cuts_at_scissors() -> Result<Option<bool>, Box<dyn std::error::Error>> {
    let Some(flags) = commit_flags() else {
        return Ok(None);
    };
    let verbose = match flags.verbose {
        Some(verbose) => verbose,
        None => config_value("commit.verbose")?.is_some_and(|v| {
            matches!(v.to_lowercase().as_str(), "true" | "yes" | "on")
                || v.parse::<i64>().is_ok_and(|n| n > 0)
        }),
    };
    let editor = std::env::var_os("GIT_EDITOR").as_deref() != Some(std::ffi::OsStr::new(":"));
    let cleanup = match flags.cleanup {
        Some(mode) => Some(mode),
        None => config_value("commit.cleanup")?,
    };
    Ok(Some(
        verbose || (editor && cleanup.as_deref() == Some("scissors")),
    ))
}

/// Refusals for the Class-Search lines Git can keep (`kept`: the message file up to the scissors line
/// Git cuts at, when it cuts). The lines are read before whitespace cleanup, because
/// `--cleanup=verbatim` keeps trailing whitespace that the range check then refuses.
fn class_search_violations(kept: &[u8]) -> Vec<String> {
    let mut violations = Vec::new();
    for (index, line) in kept.split(|b| *b == b'\n').enumerate() {
        let line = String::from_utf8_lossy(line.strip_suffix(b"\r").unwrap_or(line));
        if line.starts_with("Class-Search:") && pg_policy::parse(&line).is_none() {
            violations.push(format!(
                "line {}: malformed Class-Search: {}",
                index + 1,
                pg_policy::refusal(&line)
            ));
        }
    }
    violations
}

fn run() -> Result<i32, Box<dyn std::error::Error>> {
    let message = std::env::args_os().nth(1).ok_or("missing message file")?;
    if message == "--install" {
        let root = std::env::args_os().nth(2).ok_or("missing checkout path")?;
        return install(Path::new(&root));
    }
    let root = Command::new("git")
        .args(["rev-parse", "--show-toplevel"])
        .output()?;
    if !root.status.success() {
        return Err("cannot locate checkout".into());
    }
    // MERGE_HEAD is worktree-specific. Git also invokes commit-msg for merges.
    let merge = Command::new("git")
        .args(["rev-parse", "--verify", "-q", "MERGE_HEAD"])
        .output()?;
    let raw = std::fs::read(message)?;
    let strip_comments = strips_comments()?;
    let configured = config_value("core.commentString")?.or(config_value("core.commentChar")?);
    // Git's own cutoff decides which tail is discarded; an unknown command line cuts where comments
    // are stripped (as before) and checks every Class-Search line.
    let cuts = git_cuts_at_scissors()?;
    let marker = scissors_marker(&raw, cuts.unwrap_or(strip_comments), configured.as_deref());
    let cut = marker.map_or(raw.len(), |(offset, _)| offset);
    let class_search =
        class_search_violations(&raw[..if cuts == Some(true) { cut } else { raw.len() }]);
    if merge.status.success() {
        // A merge needs no attribution, but its Class-Search lines reach the same range check.
        for violation in &class_search {
            eprintln!("commit-msg: {violation}");
        }
        return Ok(i32::from(!class_search.is_empty()));
    }
    if merge.status.code() != Some(1) {
        return Err("cannot inspect merge state".into());
    }
    // Install snapshots the single validator source so a branch switch cannot
    // replace the installed binary's protocol with a pre-feature gate.
    let executable = std::env::current_exe()?;
    let snapshot = executable.with_file_name(".commit-msg-gate");
    let gate = if snapshot.is_file() {
        snapshot
    } else {
        executable
            .parent()
            .and_then(|p| p.parent())
            .ok_or("cannot locate installed validator")?
            .join("ci_commit_gate.py")
    };
    // Git removes editor comments after this hook runs. Validate that same
    // message, honoring core.commentChar, without rewriting the user's file.
    let mut command = Command::new("git");
    let marker_prefix = marker.map(|(_, prefix)| prefix);
    // With auto and no verbose diff, Git's generated comment block includes
    // standalone prefix lines. The last such line identifies its chosen char.
    let auto_prefix = if strip_comments && configured.as_deref() == Some("auto") {
        raw.split(|b| *b == b'\n')
            .rev()
            .find(|line| line.len() == 1 && b"#;@!$%^&|:".contains(&line[0]))
    } else {
        None
    };
    if let Some(prefix) = marker_prefix.or(auto_prefix) {
        let prefix = std::str::from_utf8(prefix)?;
        if !prefix.is_empty() {
            command
                .arg("-c")
                .arg(format!("core.commentString={prefix}"));
            if prefix.len() == 1 {
                command.arg("-c").arg(format!("core.commentChar={prefix}"));
            }
        }
    }
    command.arg("stripspace");
    if strip_comments {
        command.arg("--strip-comments");
    }
    let mut cleaner = command
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .spawn()?;
    let input = cleaner.stdin.take().ok_or("missing cleaner stdin")?;
    let cleaned = std::thread::scope(|scope| -> Result<_, std::io::Error> {
        let writer = scope.spawn(move || {
            let mut input = input;
            input.write_all(&raw[..cut])
        });
        let output = cleaner.wait_with_output();
        writer
            .join()
            .map_err(|_| std::io::Error::other("message writer panicked"))??;
        output
    })?;
    if !cleaned.status.success() {
        return Err("cannot clean commit message".into());
    }
    let mut gate = Command::new("python3")
        // This bridge exposes the installed reader without maintaining another
        // paragraph parser. All placement checks and diagnostics stay in Rust.
        .arg("-c")
        .arg(concat!(
            "import json, runpy, sys; ",
            "gate = runpy.run_path(sys.argv[1]); message = sys.stdin.read(); ",
            "print(json.dumps([gate['_trailer_block_lines'](message), ",
            "gate['message_violations'](message)]))"
        ))
        .arg(gate)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .spawn()?;
    gate.stdin
        .take()
        .ok_or("missing gate stdin")?
        .write_all(&cleaned.stdout)?;
    let output = gate.wait_with_output()?;
    if !output.status.success() {
        return Err("cannot read commit trailer policy".into());
    }
    let (block, mut violations): (Vec<String>, Vec<String>) =
        serde_json::from_slice(&output.stdout)?;
    let message = std::str::from_utf8(&cleaned.stdout)?;
    let mut remaining = message
        .lines()
        .filter(|line| !line.trim().is_empty())
        .count();
    for (index, line) in message.lines().enumerate() {
        if line.trim().is_empty() {
            continue;
        }
        if remaining > block.len() {
            if let Some((key, _)) = line.trim_start().split_once(':') {
                if ["Plan-Item", "Writer", "Hand-Edit", "Changelog", "Gate-Edit"]
                    .iter()
                    .any(|known| key.trim_end().eq_ignore_ascii_case(known))
                {
                    violations.push(format!(
                        "line {}: {} outside trailing trailer block",
                        index + 1,
                        key.trim_end()
                    ));
                }
            }
        }
        remaining -= 1;
    }
    // The range check of fast checks reads the same parser: a bad line fails here, not at review.
    violations.extend(class_search);
    for violation in &violations {
        eprintln!("commit-msg: {violation}");
    }
    Ok(i32::from(!violations.is_empty()))
}

fn main() -> ExitCode {
    match run() {
        Ok(0) => ExitCode::SUCCESS,
        Ok(_) => ExitCode::FAILURE,
        Err(error) => {
            eprintln!("commit-msg: {error}");
            ExitCode::FAILURE
        }
    }
}
