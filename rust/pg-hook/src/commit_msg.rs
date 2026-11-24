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

/// Where Git discards the message tail: the scissors line it generated for `--verbose` or an editor
/// message, which it always follows with its own two-line hint. A literal look-alike in a `-m`
/// message has no hint and is kept (also under commit.cleanup=scissors), so it is not a cut.
fn generated_scissors_offset(raw: &[u8]) -> Option<usize> {
    let hint = b" Do not modify or remove the line above.";
    let mut lines = raw
        .split_inclusive(|b| *b == b'\n')
        .scan(0, |offset, line| {
            let start = *offset;
            *offset += line.len();
            Some((start, line.strip_suffix(b"\n").unwrap_or(line)))
        });
    let mut previous = lines.next()?;
    for current in lines {
        if let Some(prefix) = previous.1.strip_suffix(SCISSORS).filter(|p| !p.is_empty()) {
            if current.1.strip_prefix(prefix) == Some(hint) {
                return Some(previous.0);
            }
        }
        previous = current;
    }
    None
}

/// Refusals for the Class-Search lines Git can keep: the whole message file except the tail below the
/// scissors line Git generated. The lines are read before whitespace cleanup, because
/// `--cleanup=verbatim` keeps trailing whitespace that the range check then refuses.
fn class_search_violations(raw: &[u8]) -> Vec<String> {
    let kept = &raw[..generated_scissors_offset(raw).unwrap_or(raw.len())];
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
    let marker = scissors_marker(&raw, strip_comments, configured.as_deref());
    let cut = marker.map_or(raw.len(), |(offset, _)| offset);
    let class_search = class_search_violations(&raw);
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
