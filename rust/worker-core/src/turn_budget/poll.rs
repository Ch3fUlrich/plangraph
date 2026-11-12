//! Narrow shell recognition, not a security parser or command sandbox.
use sha2::{Digest, Sha256};

pub fn refusal() -> String {
    "TURN-BUDGET polling refused; use `python -m plangraph.spawner.cli wait <run_id> --timeout 3600` with Bash run_in_background=true (one command that exits), then wait for its completion notification.".into()
}

#[derive(Debug)]
pub(super) struct Shell {
    pub commands: Vec<Vec<String>>,
    pub looped: bool,
    pub writes: bool,
    pub dynamic: bool,
}

fn push_word(words: &mut Vec<String>, word: &mut String, quoted: &mut bool) {
    if word.is_empty() && !*quoted {
        return;
    }
    // Preserve quote provenance until reserved-word recognition is complete.
    if *quoted {
        word.insert(0, '\0');
    }
    words.push(std::mem::take(word));
    *quoted = false;
}

/// Quoted text stays a word; comments and whitespace never become commands.
/// Unsupported/unbalanced syntax fails open rather than guessing a refusal.
pub(super) fn parse(s: &str) -> Option<Shell> {
    let mut words = Vec::new();
    let mut word = String::new();
    let mut commands = Vec::new();
    let mut chars = s.chars().peekable();
    let mut quote = None;
    let mut quoted = false;
    let mut writes = false;
    let mut dynamic = false;
    while let Some(c) = chars.next() {
        if let Some(q) = quote {
            if q == '"' && matches!(c, '$' | '`') {
                if c == '`' || chars.peek() == Some(&'(') {
                    return None;
                }
                dynamic = true;
            }
            if c == q {
                quote = None;
            } else if c == '\\' && q == '"' {
                word.push(chars.next()?);
            } else {
                word.push(c);
            }
            continue;
        }
        match c {
            // No expansion or heredoc modelling: do not mistake their contents
            // for a known read-only check or a state-changing command.
            '`' | '<' => return None,
            '$' => {
                if chars.peek() == Some(&'(') {
                    return None;
                }
                dynamic = true;
                word.push(c);
            }
            '\'' | '"' => {
                quote = Some(c);
                quoted = true;
            }
            '\\' => {
                quoted = true;
                word.push(chars.next()?);
            }
            '#' if word.is_empty() => {
                while chars.peek().is_some_and(|&c| c != '\n') {
                    chars.next();
                }
            }
            ';' | '&' | '|' | '\n' | '(' | ')' | '{' | '}' => {
                push_word(&mut words, &mut word, &mut quoted);
                if !words.is_empty() {
                    commands.push(std::mem::take(&mut words));
                }
            }
            '>' => {
                writes = true;
                // Keep executable redirections distinct from literal '>' text.
                word.push('\u{1}');
            }
            c if c.is_whitespace() => {
                push_word(&mut words, &mut word, &mut quoted);
            }
            c => word.push(c),
        }
    }
    if quote.is_some() {
        return None;
    }
    push_word(&mut words, &mut word, &mut quoted);
    if !words.is_empty() {
        commands.push(words);
    }
    let mut looped = false;
    for words in &mut commands {
        while words.first().is_some_and(|s| {
            matches!(
                s.as_str(),
                "while" | "until" | "do" | "done" | "if" | "then" | "else" | "fi" | "!"
            )
        }) {
            looped |= matches!(words[0].as_str(), "while" | "until");
            words.remove(0);
        }
        looped |= words.first().is_some_and(|s| s == "for");
        for word in words.iter_mut() {
            if word.starts_with('\0') {
                word.remove(0);
            }
        }
        if let Some(first) = words.first_mut() {
            *first = first.rsplit('/').next()?.to_owned();
        }
    }
    commands.retain(|c| !c.is_empty());
    Some(Shell {
        commands,
        looped,
        writes,
        dynamic,
    })
}

pub fn sleep_poll(command: &str) -> bool {
    sleep_poll_depth(command, 0)
}

fn sleep_poll_depth(command: &str, depth: usize) -> bool {
    if depth > 3 {
        return false;
    }
    let Some(shell) = parse(command) else {
        return false;
    };
    let has_sleep = shell.commands.iter().any(|c| c[0] == "sleep");
    let only_checks = !shell.writes
        && shell.commands.iter().any(|c| read_command(c))
        && shell
            .commands
            .iter()
            .all(|c| read_command(c) || matches!(c[0].as_str(), "true" | "false" | ":" | "for"));
    if shell.looped && (has_sleep || only_checks) {
        return true;
    }
    for (i, c) in shell.commands.iter().enumerate() {
        if c[0] == "watch" && watch_read(c) {
            return true;
        }
        if c[0] == "sleep" && shell.commands.get(i + 1).is_some_and(|c| read_command(c)) {
            return true;
        }
        if matches!(c[0].as_str(), "bash" | "sh" | "dash" | "zsh") {
            if let Some(index) = c.iter().position(|s| s == "-c" || s == "-lc") {
                if c.get(index + 1)
                    .is_some_and(|s| sleep_poll_depth(s, depth + 1))
                {
                    return true;
                }
            }
        }
    }
    false
}

fn watch_read(c: &[String]) -> bool {
    let mut i = 1;
    while let Some(arg) = c.get(i) {
        match arg.as_str() {
            "-n" | "--interval" => i += 2,
            "-d" | "--differences" | "-t" | "--no-title" | "--" => i += 1,
            _ if arg.starts_with("--interval=") => i += 1,
            _ => return read_command(&c[i..]),
        }
    }
    false
}

fn read_command(c: &[String]) -> bool {
    // --watch is a single exiting wait, not a repeated snapshot check.
    if c.iter().any(|s| s == "--watch") {
        return false;
    }
    let args: Vec<_> = c.iter().map(String::as_str).collect();
    matches!(
        args.as_slice(),
        [
            "git",
            "status" | "log" | "show" | "diff" | "rev-parse" | "ls-files",
            ..
        ] | ["gh", "pr", "checks" | "view" | "list" | "status", ..]
            | ["gh", "run", "view" | "list", ..]
            | [
                "python" | "python3",
                "-m",
                "plangraph.spawner.cli",
                "ps" | "status" | "result",
                ..
            ]
            | [
                "cat" | "head" | "tail" | "grep" | "rg" | "ls" | "stat" | "test" | "[" | "ps",
                ..
            ]
    )
}

pub fn read_only(command: &str) -> bool {
    let Some(shell) = parse(command) else {
        return false;
    };
    !shell.looped
        && !shell.writes
        && !shell.dynamic
        && !shell.commands.is_empty()
        && shell.commands.iter().all(|c| read_command(c))
}

pub fn fingerprint(command: &str, cwd: &str) -> String {
    let words = parse(command).map(|s| s.commands).unwrap_or_default();
    let mut hasher = Sha256::new();
    hasher.update(cwd.as_bytes());
    hasher.update([0]);
    hasher.update(serde_json::to_vec(&words).unwrap_or_default());
    format!("{:x}", hasher.finalize())
}
