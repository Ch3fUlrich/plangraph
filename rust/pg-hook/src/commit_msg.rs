//! Git entry point. Trailer block parsing and value policy live in ci_commit_gate.py.
#[path = "commit_args.rs"]
mod commit_args;

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

/// The mode Git cleans this message with: the `--cleanup` option over `commit.cleanup`.
fn cleanup_mode(
    args: Option<&commit_args::CommitArgs>,
) -> Result<Option<String>, Box<dyn std::error::Error>> {
    match args.and_then(|a| a.cleanup.clone()) {
        Some(mode) => Ok(mode),
        None => config_value("commit.cleanup"),
    }
}

/// Whether Git opens an editor: from the command line, else from the GIT_EDITOR=: sentinel Git sets
/// when it opens none (an explicit `:` editor is told apart by the command line).
fn editor_used(args: Option<&commit_args::CommitArgs>) -> bool {
    args.map_or_else(
        || std::env::var_os("GIT_EDITOR").as_deref() != Some(std::ffi::OsStr::new(":")),
        commit_args::CommitArgs::uses_editor,
    )
}

/// Whether Git strips comments of this commit: always for strip and scissors cleanup, never for
/// whitespace and verbatim, and for the default mode when an editor is used.
fn strips_comments(cleanup: Option<&str>, editor: bool) -> bool {
    match cleanup {
        Some("strip" | "scissors") => true,
        Some("whitespace" | "verbatim") => false,
        _ => editor,
    }
}

/// The comment character Git chose for `core.commentChar=auto`. With an editor the generated comment
/// block holds standalone prefix lines (the last identifies the choice); without one Git picks the
/// first candidate that starts no message line, so a literal marker line never carries it.
fn auto_comment_char(raw: &[u8], editor: bool) -> Option<&[u8]> {
    if !editor {
        return None;
    }
    raw.split(|b| *b == b'\n')
        .rev()
        .find(|line| line.len() == 1 && b"#;@!$%^&|:".contains(&line[0]))
}

/// The first scissors line Git will cut at, as (offset, comment prefix): the line is the comment
/// string followed by the fixed scissors text. A one-character comment character is the only
/// prefix; `auto` is the character Git chose (`auto_char`). A literal look-alike is kept by Git.
fn scissors_marker<'a>(
    raw: &'a [u8],
    cuts: bool,
    configured: Option<&str>,
    auto_char: Option<&[u8]>,
) -> Option<(usize, &'a [u8])> {
    let prefix_ok = |prefix: &[u8]| match configured {
        None => prefix == b"#",
        Some("auto") => Some(prefix) == auto_char,
        // Git before 2.45 ignores a multi-character core.commentString and picks a character itself.
        Some(configured) if configured.len() > 1 => {
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
            (cuts && prefix_ok(prefix)).then_some((start, prefix))
        })
}

/// Whether Git cuts the message at its scissors line: with `--verbose` or `commit.verbose`, or for an
/// editor message under scissors cleanup (without an editor that mode falls back to whitespace
/// cleanup). `None` when the command line of the parent `git commit` is unknown.
fn git_cuts_at_scissors(
    args: Option<&commit_args::CommitArgs>,
    cleanup: Option<&str>,
    editor: bool,
) -> Result<Option<bool>, Box<dyn std::error::Error>> {
    let Some(args) = args else {
        return Ok(None);
    };
    let verbose = match args.verbose {
        Some(verbose) => verbose,
        None => config_value("commit.verbose")?.is_some_and(|v| {
            matches!(v.to_lowercase().as_str(), "true" | "yes" | "on")
                || v.parse::<i64>().is_ok_and(|n| n > 0)
        }),
    };
    Ok(Some(verbose || (editor && cleanup == Some("scissors"))))
}

/// Refusals for the Class-Search lines of `retained`, the message as Git saves it: cleanup applied
/// (comment lines, trailing whitespace and the scissors tail gone), so a line is judged as the range
/// check will read it.
fn class_search_violations(retained: &[u8]) -> Vec<String> {
    let mut violations = Vec::new();
    for (index, line) in retained.split(|b| *b == b'\n').enumerate() {
        let line = String::from_utf8_lossy(line);
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
    // Git's own options decide the cleanup: the command line of the parent `git commit` over the
    // config; an unknown parent falls back to the GIT_EDITOR=: sentinel and the config.
    let args = commit_args::parent();
    let editor = editor_used(args.as_ref());
    let cleanup = cleanup_mode(args.as_ref())?;
    let strip_comments = strips_comments(cleanup.as_deref(), editor);
    let configured = config_value("core.commentString")?.or(config_value("core.commentChar")?);
    let auto_char = auto_comment_char(&raw, editor);
    // Git cuts at its scissors line for --verbose, or for an editor message under scissors cleanup;
    // an unknown command line cuts where comments are stripped.
    let cuts = git_cuts_at_scissors(args.as_ref(), cleanup.as_deref(), editor)?;
    let marker = scissors_marker(
        &raw,
        cuts.unwrap_or(strip_comments),
        configured.as_deref(),
        auto_char,
    );
    let cut = marker.map_or(raw.len(), |(offset, _)| offset);
    // Git removes editor comments after this hook runs. Validate that same
    // message, honoring core.commentChar, without rewriting the user's file.
    let mut command = Command::new("git");
    let marker_prefix = marker.map(|(_, prefix)| prefix);
    // With auto, Git's generated comment block holds standalone prefix lines (see auto_comment_char).
    let auto_prefix = if strip_comments && configured.as_deref() == Some("auto") {
        auto_char
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
    let kept = &raw[..cut];
    let cleaned = std::thread::scope(|scope| -> Result<_, std::io::Error> {
        let writer = scope.spawn(move || {
            let mut input = input;
            input.write_all(kept)
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
    // The range check of fast checks reads the same parser: a bad line fails here, not at review.
    // Verbatim cleanup keeps the text as written; every other mode saves the cleaned message.
    let retained: &[u8] = if cleanup.as_deref() == Some("verbatim") {
        kept
    } else {
        &cleaned.stdout
    };
    let class_search = class_search_violations(retained);
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
