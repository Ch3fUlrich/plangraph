//! Only transparent, literal wrappers; unknown options are not guessed.
pub(super) fn peel(command: &[String]) -> Vec<String> {
    let mut c = command;
    for _ in 0..16 {
        let program = c[0].rsplit('/').next().unwrap_or(&c[0]);
        let mut i = 1;
        let mut duration = false;
        let wrapped = matches!(program, "timeout" | "env" | "nice" | "nohup" | "command");
        if !wrapped {
            break;
        }
        while let Some(arg) = c.get(i) {
            if arg == "--" {
                i += 1;
                break;
            }
            let skip = match (program, arg.as_str()) {
                ("timeout", "-s" | "--signal" | "-k" | "--kill-after")
                | ("env", "-u" | "--unset")
                | ("nice", "-n" | "--adjustment") => 2,
                ("timeout", "--preserve-status" | "--foreground" | "-v" | "--verbose")
                | ("env", "-i" | "--ignore-environment")
                | ("command", "-p") => 1,
                ("timeout", s) if s.starts_with("--signal=") || s.starts_with("--kill-after=") => 1,
                ("env", s)
                    if s.starts_with("--unset=") || (!s.starts_with('-') && s.contains('=')) =>
                {
                    1
                }
                ("nice", s)
                    if s.starts_with("--adjustment=")
                        || s.strip_prefix('-').is_some_and(|n| {
                            !n.is_empty() && n.bytes().all(|b| b.is_ascii_digit())
                        }) =>
                {
                    1
                }
                (_, s) if s.starts_with('-') => return normalized(c),
                ("timeout", _) => {
                    duration = true;
                    i += 1;
                    break;
                }
                _ => break,
            };
            i += skip;
        }
        if program == "timeout" && !duration {
            i += 1; // duration after an explicit --
        }
        if i >= c.len() {
            break;
        }
        c = &c[i..];
    }
    normalized(c)
}

fn normalized(c: &[String]) -> Vec<String> {
    let mut c = c.to_vec();
    c[0] = c[0].rsplit('/').next().unwrap_or(&c[0]).to_owned();
    c
}

pub(super) fn script(c: &[String]) -> Option<&str> {
    if !matches!(c[0].as_str(), "bash" | "sh" | "dash" | "zsh") {
        return None;
    }
    let index = c.iter().position(|s| s == "-c" || s == "-lc")?;
    c.get(index + 1).map(String::as_str)
}

// Keep expansion bodies for a separate poll scan and retain surrounding words.
pub(super) fn skip_expansion(
    c: char,
    chars: &mut std::iter::Peekable<std::str::Chars<'_>>,
) -> String {
    let mut body = String::new();
    if c == '`' {
        for c in chars.by_ref() {
            if c == '`' {
                break;
            }
            body.push(c);
        }
        return body;
    }
    chars.next(); // opening parenthesis after $
    let mut depth = 1usize;
    let mut quote = None;
    while let Some(c) = chars.next() {
        body.push(c);
        if c == '\\' {
            if let Some(c) = chars.next() {
                body.push(c);
            }
        } else if quote == Some(c) {
            quote = None;
        } else if quote.is_none() {
            match c {
                '\'' | '"' => quote = Some(c),
                '(' => depth += 1,
                ')' => {
                    depth -= 1;
                    if depth == 0 {
                        body.pop();
                        break;
                    }
                }
                _ => (),
            }
        }
    }
    body
}

pub(super) fn closing_quote(
    mut chars: std::iter::Peekable<std::str::Chars<'_>>,
    quote: char,
) -> bool {
    while let Some(c) = chars.next() {
        if c == quote {
            return true;
        }
        if c == '\\' && quote == '"' {
            chars.next();
        }
    }
    false
}

pub(super) fn skip_heredoc(chars: &mut std::iter::Peekable<std::str::Chars<'_>>) -> String {
    chars.next(); // second <
    let header: String = chars.by_ref().take_while(|&c| c != '\n').collect();
    let (header, executable) = header.split_once(';').unwrap_or((&header, ""));
    let delimiter = header
        .trim()
        .trim_start_matches('-')
        .trim()
        .trim_matches(['\'', '"']);
    loop {
        let line: String = chars.by_ref().take_while(|&c| c != '\n').collect();
        if line.trim() == delimiter || chars.peek().is_none() {
            break;
        }
    }
    executable.to_owned()
}

pub(super) fn assignment(word: &str) -> bool {
    word.split_once('=').is_some_and(|(name, _)| {
        !name.is_empty()
            && !name.starts_with(|c: char| c.is_ascii_digit())
            && name.chars().all(|c| c.is_ascii_alphanumeric() || c == '_')
    })
}

// Redirection operands are not command names, including leading redirects.
pub(super) fn skip_redirect(chars: &mut std::iter::Peekable<std::str::Chars<'_>>) {
    while chars
        .peek()
        .is_some_and(|c| matches!(c, '>' | '<' | '&' | '|'))
    {
        chars.next();
    }
    while chars
        .peek()
        .is_some_and(|c| c.is_whitespace() && *c != '\n')
    {
        chars.next();
    }
    let mut quote = None;
    while let Some(&c) = chars.peek() {
        if quote.is_none() && (c.is_whitespace() || matches!(c, ';' | '&' | '|')) {
            break;
        }
        chars.next();
        if quote == Some(c) {
            quote = None;
        } else if quote.is_none() && matches!(c, '\'' | '"') {
            quote = Some(c);
        }
    }
}

pub(super) fn normalize_paths(c: &mut [String]) {
    let program = c[0].clone();
    let start = match program.as_str() {
        "cat" | "head" | "tail" | "ls" | "stat" | "test" | "[" => 1,
        "grep" | "rg" => 2, // the first operand is a pattern, not a path
        "git"
            if matches!(
                c.get(1).map(String::as_str),
                Some("status" | "diff" | "ls-files")
            ) =>
        {
            2
        }
        "git" => match c.iter().position(|s| s == "--") {
            Some(i) => i + 1,
            None => return,
        },
        _ => return,
    };
    // String/numeric test operands are not file paths. Only unary file checks
    // are normalised; arbitrary test expressions need a real expression parser.
    if matches!(program.as_str(), "test" | "[")
        && !(matches!(
            c.get(1).map(String::as_str),
            Some("-e" | "-f" | "-d" | "-s" | "-r" | "-w" | "-x" | "-L" | "-h")
        ) && (c.len() == 3 || (c.len() == 4 && c[3] == "]")))
    {
        return;
    }
    let mut pattern = matches!(program.as_str(), "grep" | "rg");
    let mut option_value = false;
    for arg in c.iter_mut().skip(if pattern { 1 } else { start }) {
        if option_value {
            option_value = false;
            continue;
        }
        if arg.starts_with('-') {
            option_value = match program.as_str() {
                "grep" | "rg" => matches!(
                    arg.as_str(),
                    "-e" | "--regexp"
                        | "-f"
                        | "--file"
                        | "-g"
                        | "--glob"
                        | "--iglob"
                        | "-t"
                        | "--type"
                        | "--type-not"
                        | "-m"
                        | "--max-count"
                        | "-A"
                        | "-B"
                        | "-C"
                        | "--context"
                ),
                "head" | "tail" => matches!(arg.as_str(), "-n" | "-c" | "--lines" | "--bytes"),
                "stat" => matches!(arg.as_str(), "-c" | "--format" | "--printf"),
                "ls" => matches!(
                    arg.as_str(),
                    "-I" | "--ignore"
                        | "--hide"
                        | "--format"
                        | "--sort"
                        | "--time"
                        | "--time-style"
                        | "--block-size"
                        | "--color"
                ),
                _ => false,
            };
            if matches!(arg.as_str(), "-e" | "--regexp" | "-f" | "--file") {
                pattern = false;
            }
            continue;
        }
        if pattern {
            pattern = false;
            continue;
        }
        if arg == "]" {
            continue;
        }
        // Components collapse literal ./ and repeated separators without resolving
        // symlinks or changing parent-directory semantics.
        let path: std::path::PathBuf = std::path::Path::new(arg)
            .components()
            .filter(|part| *part != std::path::Component::CurDir)
            .collect();
        *arg = if path.as_os_str().is_empty() {
            ".".into()
        } else {
            path.to_string_lossy().into_owned()
        };
    }
}
