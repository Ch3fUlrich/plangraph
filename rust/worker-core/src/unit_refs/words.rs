//! Parsing of unit-file text into the absolute paths it names.
//!
//! Contract: PROTECTION reads every piece of every word split on whitespace, quotes and
//! `; & | ( ) < > = , :` (over-protection is accepted: a shell script, a comment or a redirection
//! still yields its paths) plus every whole word that starts with `/`. The REPORT (`exact`) names
//! only whole words that start with `/` and hold none of whitespace `; & | ( ) < >`, and the
//! literal value of a path-valued unit field. Nothing here parses shell.
use std::path::{Path, PathBuf};

/// Words of one unit line the way systemd splits them: whitespace separates, double and single
/// quotes group, a backslash escapes (`\x20` is a space).
fn split_words(line: &str) -> Vec<String> {
    let mut words = Vec::new();
    let (mut word, mut open, mut quote) = (String::new(), false, None::<char>);
    let mut chars = line.chars().peekable();
    while let Some(c) = chars.next() {
        match (c, quote) {
            ('\\', _) => match chars.next() {
                Some('x') => {
                    let hex: String = (0..2)
                        .filter_map(|_| chars.next_if(char::is_ascii_hexdigit))
                        .collect();
                    match u8::from_str_radix(&hex, 16) {
                        Ok(byte) if hex.len() == 2 => word.push(byte as char),
                        _ => word.push_str(&format!("\\x{hex}")),
                    }
                    open = true;
                }
                Some(next) => {
                    // systemd's C escapes: \s is a space, \t a tab, ...; anything else is itself.
                    word.push(match next {
                        's' => ' ',
                        't' => '\t',
                        'n' => '\n',
                        'r' => '\r',
                        'a' => '\x07',
                        'b' => '\x08',
                        'f' => '\x0c',
                        'v' => '\x0b',
                        other => other,
                    });
                    open = true;
                }
                None => {}
            },
            (q, Some(open_quote)) if q == open_quote => quote = None,
            ('"' | '\'', None) => {
                quote = Some(c);
                open = true;
            }
            (c, None) if c.is_whitespace() => {
                if open {
                    words.push(std::mem::take(&mut word));
                    open = false;
                }
            }
            (c, _) => {
                word.push(c);
                open = true;
            }
        }
    }
    if open {
        words.push(word);
    }
    words
}

/// Characters that end a piece of a word for protection.
fn is_piece_break(c: char) -> bool {
    c.is_whitespace() || "\"'&;|()<>=,:".contains(c)
}

/// A whole word the report may name: it starts with `/` and holds no whitespace or `; & | ( ) < >`.
pub(super) fn is_whole_word_path(word: &str) -> bool {
    word.starts_with('/') && !word.contains(|c: char| c.is_whitespace() || ";&|()<>".contains(c))
}

/// Unit keys whose value is one literal path, spaces and all (`WorkingDirectory=/x/team blue`).
fn is_single_path_key(key: &str) -> bool {
    matches!(
        key,
        "WorkingDirectory" | "RootDirectory" | "RootImage" | "EnvironmentFile" | "PIDFile"
    ) || [
        "Path",
        "Directory",
        "File",
        "Exists",
        "Changed",
        "Modified",
        "NotEmpty",
    ]
    .iter()
    .any(|suffix| key.ends_with(suffix))
}

/// Unit keys whose value is a list of paths, one per word (`ReadWritePaths=/a /b`).
fn is_path_list_key(key: &str) -> bool {
    key.ends_with("Paths")
}

/// A piece as a path: `%%` is a literal percent, any other `%` is an unresolved specifier
/// (`%n`, `%i`, ...) that names nothing.
fn path_of(piece: &str) -> Option<PathBuf> {
    let piece = piece
        .trim_start_matches(['-', '@', '+', '!'])
        .trim_end_matches('/');
    if piece.len() < 2 || !piece.starts_with('/') {
        return None;
    }
    let mut path = String::with_capacity(piece.len());
    let mut chars = piece.chars();
    while let Some(c) = chars.next() {
        if c != '%' {
            path.push(c);
        } else if chars.next() == Some('%') {
            path.push('%');
        } else {
            return None;
        }
    }
    Some(PathBuf::from(path))
}

/// Paths of the unit text, with whether each is exact (reportable); see the module contract.
pub(super) fn text_paths(text: &str, home: &Path) -> Vec<(PathBuf, bool)> {
    let mut paths = Vec::new();
    // A trailing backslash continues the line.
    let joined = text.replace("\\\n", " ");
    for line in joined.lines().map(str::trim_start) {
        if line.starts_with('#') || line.starts_with(';') {
            continue;
        }
        let line = line.replace("%h", &home.to_string_lossy());
        // `Key = value`: the key ends at the first `=`; an Exec value may start with `-@+!:`.
        let (key, value) = match line.split_once('=') {
            Some((key, value))
                if !key.trim().is_empty()
                    && key
                        .trim()
                        .bytes()
                        .all(|b| b.is_ascii_alphanumeric() || b == b'_') =>
            {
                (key.trim(), value.trim_start())
            }
            _ => ("", line.as_str()),
        };
        let value = if key.starts_with("Exec") {
            value.trim_start_matches(['-', '@', '+', '!', ':'])
        } else {
            value
        };
        let single = is_single_path_key(key);
        if single {
            // One literal path: the whole value, unquoted or quoted, exact.
            let whole = value.trim().trim_start_matches('-');
            let whole = whole
                .strip_prefix('"')
                .and_then(|w| w.strip_suffix('"'))
                .or_else(|| whole.strip_prefix('\'').and_then(|w| w.strip_suffix('\'')))
                .unwrap_or(whole);
            paths.extend(path_of(whole).map(|path| (path, true)));
        }
        let list_key = is_path_list_key(key);
        for word in split_words(value) {
            // Report: a whole word, or a word of a path-list field.
            // (A single-valued field's words are pieces of its whole value, already exact above.)
            if !single && (is_whole_word_path(&word) || (list_key && word.starts_with('/'))) {
                paths.extend(path_of(&word).map(|path| (path, true)));
            } else {
                // A word with spaces may still be one path: protect it whole as well.
                paths.extend(path_of(assignment_value(&word)).map(|path| (path, false)));
            }
            for piece in word.split(is_piece_break) {
                paths.extend(path_of(piece).map(|path| (path, false)));
            }
        }
    }
    paths
}

/// `Key=NAME=value` words carry the path after the last assignment: `ROOT=/wt/x y`.
fn assignment_value(word: &str) -> &str {
    let mut value = word;
    while let Some((name, rest)) = value.split_once('=') {
        if name.is_empty() || name.contains('/') {
            break;
        }
        value = rest;
    }
    value
}
