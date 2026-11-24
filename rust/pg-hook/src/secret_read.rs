//! Refuse to print a key, token or env file into a session's own output (PreToolUse guard).
//!
//! A fleet session runs with bypassed permissions, so nothing stops `cat ~/.config/plangraph/*`
//! from putting every secret of that directory into the transcript that goes to the model
//! provider. This guard reads the Bash command or the Read tool call, resolves each path
//! (tilde, `$HOME`, relative, `..`, globs against the file system) and refuses a printing verb
//! on a path under the credential list of `src/plangraph/config/secret_paths.json`. Checking a
//! file stays possible: `ls`, `stat`, `test -s` and `wc -c` are not printing verbs.
//!
//! It is a gate for one accident, not a sandbox: it reads the command text and does not model
//! variables other than the home and config-home ones, functions, or a script that opens the
//! file itself.
use pg_worker_core::credential_isolation::session_guard_paths;
use serde_json::Value;
use std::io::{BufRead, Write};
use std::path::{Component, Path, PathBuf};

/// Verbs that print the content of their file arguments.
const PRINT_VERBS: &[&str] = &[
    "cat", "tac", "head", "tail", "less", "more", "most", "nl", "od", "xxd", "hexdump", "strings",
    "base64", "sed", "awk", "gawk", "mawk", "bat", "jq", "yq", "cut", "sort", "uniq", "paste",
    "column", "fold", "diff", "cmp", "comm", "zcat", "source", ".", "grep", "egrep", "fgrep", "rg",
    "ag",
];
/// Verbs whose input redirect from a secret file prints nothing.
const QUIET_VERBS: &[&str] = &[
    "wc",
    "test",
    "[",
    "[[",
    "stat",
    "ls",
    "file",
    "sha256sum",
    "sha1sum",
    "md5sum",
    "cksum",
    "true",
    ":",
    "chmod",
    "chown",
];
/// Verbs that run their arguments as a command.
const WRAPPERS: &[&str] = &[
    "sudo", "env", "command", "builtin", "exec", "time", "nice", "nohup", "timeout", "xargs",
    "stdbuf", "setsid", "ionice",
];
const MAX_DEPTH: usize = 3;
const MAX_WALK: usize = 4096;

/// The host facts a path check needs.
pub struct Context {
    home: PathBuf,
    xdg: PathBuf,
    cwd: PathBuf,
    deny: Vec<PathBuf>,
}

impl Context {
    pub fn new(home: &Path, xdg_config_home: Option<&Path>, cwd: &Path) -> Self {
        let mut deny = session_guard_paths(home, xdg_config_home);
        // A symlinked home must not hide a denied path: add the resolved spelling too.
        let resolved: Vec<PathBuf> = deny
            .iter()
            .filter_map(|path| std::fs::canonicalize(path).ok())
            .collect();
        deny.extend(resolved);
        Context {
            home: home.to_path_buf(),
            xdg: xdg_config_home
                .map(Path::to_path_buf)
                .unwrap_or_else(|| home.join(".config")),
            cwd: cwd.to_path_buf(),
            deny,
        }
    }

    fn expand(&self, word: &str) -> String {
        let home = self.home.to_string_lossy();
        let xdg = self.xdg.to_string_lossy();
        let mut text = word.to_owned();
        if text == "~" || text.starts_with("~/") {
            text = format!("{home}{}", &text[1..]);
        }
        for (name, value) in [("HOME", &home), ("XDG_CONFIG_HOME", &xdg)] {
            text = text
                .replace(&format!("${{{name}}}"), value)
                .replace(&format!("${name}"), value);
        }
        text
    }

    fn denied(&self, path: &Path) -> bool {
        self.deny.iter().any(|denied| path.starts_with(denied))
    }

    /// The first existing or literal path a word names that lies under the credential list.
    fn secret_in(&self, word: &str) -> Option<PathBuf> {
        let word = word
            .split_once('=')
            .filter(|_| word.starts_with('-'))
            .map_or(word, |(_, v)| v);
        let text = self.expand(word);
        if text.is_empty() || text.starts_with('-') {
            return None;
        }
        let path = normalize(&self.cwd.join(&text));
        if has_glob(&text) {
            let hits = expand_glob(&path);
            if let Some(hit) = hits.iter().find(|hit| self.denied(hit)) {
                return Some(hit.clone());
            }
            // An empty expansion under a denied directory still names that directory.
            let literal: PathBuf = path
                .components()
                .take_while(|c| !has_glob(&c.as_os_str().to_string_lossy()))
                .collect();
            return self.denied(&literal).then_some(literal);
        }
        if self.denied(&path) {
            return Some(path);
        }
        std::fs::canonicalize(&path)
            .ok()
            .filter(|real| self.denied(real))
            .map(|_| path)
    }
}

fn has_glob(text: &str) -> bool {
    text.contains(['*', '?', '['])
}

/// Remove `.` and resolve `..` lexically.
fn normalize(path: &Path) -> PathBuf {
    let mut out = PathBuf::new();
    for component in path.components() {
        match component {
            Component::CurDir => {}
            Component::ParentDir => {
                out.pop();
            }
            other => out.push(other.as_os_str()),
        }
    }
    out
}

/// Expand a glob against the file system, one component at a time, within a visit budget.
fn expand_glob(pattern: &Path) -> Vec<PathBuf> {
    let mut paths = vec![PathBuf::new()];
    let mut budget = MAX_WALK;
    for component in pattern.components() {
        let name = component.as_os_str().to_string_lossy().into_owned();
        let mut next = Vec::new();
        for base in &paths {
            if !has_glob(&name) {
                next.push(base.join(&name));
                continue;
            }
            let Ok(matcher) = globset::Glob::new(&name).map(|glob| glob.compile_matcher()) else {
                continue;
            };
            let Ok(entries) = std::fs::read_dir(base) else {
                continue;
            };
            for entry in entries.flatten() {
                budget = budget.saturating_sub(1);
                if budget == 0 {
                    return next;
                }
                let entry_name = entry.file_name();
                if matcher.is_match(Path::new(&entry_name)) {
                    next.push(base.join(entry_name));
                }
            }
        }
        paths = next;
    }
    paths.retain(|path| path.exists());
    paths
}

/// Split a command into simple commands of words. Operators and substitutions start a new
/// command; a bare `<` is kept as its own word so the redirect target can be checked.
fn simple_commands(text: &str) -> Vec<Vec<String>> {
    let mut commands: Vec<Vec<String>> = vec![Vec::new()];
    let (mut word, mut in_word) = (String::new(), false);
    let (mut single, mut double, mut escaped) = (false, false, false);
    let chars: Vec<char> = text.chars().collect();
    let mut index = 0;
    macro_rules! flush {
        () => {
            if in_word {
                commands.last_mut().unwrap().push(std::mem::take(&mut word));
            }
            in_word = false;
        };
    }
    macro_rules! boundary {
        () => {{
            flush!();
            if !commands.last().unwrap().is_empty() {
                commands.push(Vec::new());
            }
        }};
    }
    while index < chars.len() {
        let c = chars[index];
        let next = chars.get(index + 1).copied();
        index += 1;
        if escaped {
            word.push(c);
            in_word = true;
            escaped = false;
        } else if single {
            if c == '\'' {
                single = false;
            } else {
                word.push(c);
            }
        } else if c == '\\' {
            escaped = true;
        } else if c == '`' || (c == '$' && next == Some('(')) {
            boundary!();
            if c == '$' {
                index += 1;
            }
        } else if double {
            if c == '"' {
                double = false;
            } else {
                word.push(c);
            }
        } else if c == '\'' {
            single = true;
            in_word = true;
        } else if c == '"' {
            double = true;
            in_word = true;
        } else if matches!(c, ';' | '&' | '|' | '\n' | '(' | ')' | '{' | '}')
            && (c != '{' && c != '}' || !in_word)
        {
            if c == '&' && next == Some('>') {
                continue;
            }
            boundary!();
        } else if c == '<' && next != Some('<') && next != Some('(') {
            flush!();
            commands.last_mut().unwrap().push("<".into());
        } else if c == '>' {
            flush!();
            commands.last_mut().unwrap().push(">".into());
        } else if c.is_whitespace() {
            flush!();
        } else {
            word.push(c);
            in_word = true;
        }
    }
    if in_word {
        commands.last_mut().unwrap().push(word);
    }
    commands.retain(|command| !command.is_empty());
    commands
}

fn is_assignment(word: &str) -> bool {
    word.split_once('=').is_some_and(|(name, _)| {
        !name.is_empty()
            && name.chars().all(|c| c.is_ascii_alphanumeric() || c == '_')
            && !name.starts_with(|c: char| c.is_ascii_digit())
    })
}

fn base(word: &str) -> &str {
    word.rsplit('/').next().unwrap_or(word)
}

/// Short options that take a value: the rest of their cluster, or else the next word, is that
/// value and never a flag.
fn short_takes_value(verb: &str, flag: char) -> bool {
    let set = if verb.ends_with("grep") {
        "efmABCdD"
    } else {
        "efmABCgtTjMEr"
    };
    set.contains(flag)
}

/// Short flags that make the executable print only names or counts. They differ: grep's `-L`
/// lists files without a match, ripgrep's `-L` follows symlinks and still prints matches, and
/// ripgrep has no quiet flag outside `-l`, `-c` and `-q`.
fn quiet_short_flag(verb: &str, flag: char) -> bool {
    if verb.ends_with("grep") {
        matches!(flag, 'l' | 'L' | 'c' | 'q')
    } else if verb == "ag" {
        matches!(flag, 'l' | 'L' | 'c')
    } else {
        matches!(flag, 'l' | 'c' | 'q')
    }
}

const LONG_WITH_VALUE: &[&str] = &[
    "--regexp",
    "--file",
    "--max-count",
    "--after-context",
    "--before-context",
    "--context",
    "--binary-files",
    "--devices",
    "--directories",
    "--include",
    "--exclude",
    "--exclude-dir",
    "--exclude-from",
    "--label",
    "--glob",
    "--type",
    "--type-not",
    "--replace",
    "--threads",
];

/// A grep that prints no line: it lists names or counts. Option values (`-e PATTERN`, an attached
/// `-ePATTERN`, `--regexp=PATTERN`) are skipped, so a pattern never counts as a flag.
fn grep_is_quiet(verb: &str, args: &[String]) -> bool {
    let (mut quiet, mut index) = (false, 0);
    while index < args.len() {
        let arg = &args[index];
        index += 1;
        if arg == "--" {
            break;
        }
        if let Some(long) = arg.strip_prefix("--") {
            let name = format!("--{}", long.split('=').next().unwrap_or_default());
            if matches!(
                name.as_str(),
                "--files-with-matches"
                    | "--files-without-match"
                    | "--count"
                    | "--count-matches"
                    | "--quiet"
                    | "--silent"
            ) {
                quiet = true;
            } else if LONG_WITH_VALUE.contains(&name.as_str()) && !arg.contains('=') {
                index += 1;
            }
        } else if arg.len() > 1 && arg.starts_with('-') {
            let mut cluster = arg[1..].chars();
            while let Some(flag) = cluster.next() {
                if short_takes_value(verb, flag) {
                    if cluster.as_str().is_empty() {
                        index += 1;
                    }
                    break;
                }
                quiet |= quiet_short_flag(verb, flag);
            }
        }
    }
    quiet
}

fn refusal(verb: &str, file: &Path, ctx: &Context) -> String {
    let dir = ctx
        .deny
        .iter()
        .find(|denied| file.starts_with(denied))
        .map_or_else(|| file.to_path_buf(), Clone::clone);
    format!(
        "secret-read: refused: {verb} would print {} (under {}), which holds keys, tokens or env \
         files. Check a file with test -s FILE, stat FILE or wc -c FILE; list names with ls; \
         never print the directory.",
        file.display(),
        dir.display()
    )
}

/// The refusal for `command`, or `None` to allow.
pub fn check_command(command: &str, ctx: &Context) -> Option<String> {
    check_depth(command, ctx, 0)
}

fn check_depth(command: &str, ctx: &Context, depth: usize) -> Option<String> {
    for words in simple_commands(command) {
        let mut at = 0;
        while words.get(at).is_some_and(|w| is_assignment(w)) {
            at += 1;
        }
        // Skip wrappers and their options to the command they run.
        while words.get(at).is_some_and(|w| WRAPPERS.contains(&base(w))) {
            at += 1;
            while words.get(at).is_some_and(|w| {
                w.starts_with('-') || is_assignment(w) || w.chars().all(|c| c.is_ascii_digit())
            }) {
                at += 1;
            }
        }
        let Some(verb) = words.get(at).map(|w| base(w)) else {
            continue;
        };
        let args = &words[at + 1..];
        if depth < MAX_DEPTH && matches!(verb, "bash" | "sh" | "zsh" | "dash" | "ksh") {
            let script = args.iter().skip_while(|a| *a != "-c").nth(1);
            if let Some(reason) = script.and_then(|s| check_depth(s, ctx, depth + 1)) {
                return Some(reason);
            }
        }
        if depth < MAX_DEPTH && verb == "eval" {
            if let Some(reason) = check_depth(&args.join(" "), ctx, depth + 1) {
                return Some(reason);
            }
        }
        let grep_family = verb.ends_with("grep") || matches!(verb, "rg" | "ag");
        let prints = if grep_family {
            !grep_is_quiet(verb, args)
        } else {
            PRINT_VERBS.contains(&verb)
        };
        let find_exec = verb == "find"
            && args
                .iter()
                .any(|a| matches!(a.as_str(), "-exec" | "-execdir" | "-ok" | "-okdir"));
        let mut skip_target = false;
        for (index, word) in args.iter().enumerate() {
            if skip_target {
                skip_target = false;
                continue;
            }
            if word == ">" {
                skip_target = true;
                continue;
            }
            let redirect = word == "<";
            let target = if redirect {
                args.get(index + 1)
            } else {
                Some(word)
            };
            let Some(target) = target else { continue };
            let reads = if redirect {
                !QUIET_VERBS.contains(&verb)
            } else {
                prints || find_exec
            };
            if reads {
                if let Some(file) = ctx.secret_in(target) {
                    return Some(refusal(verb, &file, ctx));
                }
            }
        }
        // `< file` before the verb, as in `< file cat`, or alone as in `$(< file)`.
        for (index, word) in words[..at].iter().enumerate() {
            if word == "<" {
                if let Some(file) = words.get(index + 1).and_then(|t| ctx.secret_in(t)) {
                    return Some(refusal("redirect", &file, ctx));
                }
            }
        }
        if words.first().is_some_and(|w| w == "<") {
            if let Some(file) = words.get(1).and_then(|t| ctx.secret_in(t)) {
                return Some(refusal("redirect", &file, ctx));
            }
        }
    }
    None
}

/// The refusal for one tool call, or `None` to allow. `Read` and a `Grep` path print content;
/// `Glob` lists names and stays allowed.
pub fn check_tool(tool: &str, input: &Value, ctx: &Context) -> Option<String> {
    let key = match tool {
        "Bash" | "exec_command" => {
            let command = input["command"]
                .as_str()
                .or_else(|| input["cmd"].as_str())?;
            return check_command(command, ctx);
        }
        "Read" => "file_path",
        "Grep" => "path",
        _ => return None,
    };
    let path = input[key].as_str()?;
    ctx.secret_in(path).map(|file| refusal(tool, &file, ctx))
}

/// Entry of the `pg-hook secret-read` subcommand: PreToolUse payload on `input`, refusal text on
/// `diagnostic`, exit code 2 on a refusal and 0 otherwise (an unreadable payload is not a call).
pub fn hook(input: impl BufRead, diagnostic: &mut impl Write) -> i32 {
    let home = std::env::var_os("HOME")
        .or_else(|| std::env::var_os("USERPROFILE"))
        .map(PathBuf::from);
    let xdg = std::env::var_os("XDG_CONFIG_HOME").map(PathBuf::from);
    match home {
        Some(home) => run_with(input, diagnostic, &home, xdg.as_deref()),
        None => 0,
    }
}

fn run_with(
    input: impl BufRead,
    diagnostic: &mut impl Write,
    home: &Path,
    xdg: Option<&Path>,
) -> i32 {
    let Ok(payload) = serde_json::from_reader::<_, Value>(input) else {
        return 0;
    };
    if payload["hook_event_name"] != "PreToolUse" {
        return 0;
    }
    let cwd = payload["cwd"]
        .as_str()
        .map_or_else(|| home.to_path_buf(), PathBuf::from);
    let ctx = Context::new(home, xdg, &cwd);
    let Some(tool) = payload["tool_name"].as_str() else {
        return 0;
    };
    match check_tool(tool, &payload["tool_input"], &ctx) {
        Some(reason) => {
            let _ = writeln!(diagnostic, "{reason}");
            2
        }
        None => 0,
    }
}

#[cfg(test)]
use serde_json::json;
#[cfg(test)]
#[path = "secret_read_tests.rs"]
mod tests;
