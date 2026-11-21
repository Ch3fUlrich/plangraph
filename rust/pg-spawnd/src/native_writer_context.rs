//! Evidence for the item-context block of the native review brief, read from the writer
//! brief (feeder or batch layout) and the exact diff.
use std::collections::{BTreeMap, BTreeSet};

const MAX_CALLERS_PER_SYMBOL: usize = 3;

fn heading(line: &str) -> bool {
    let line = line.trim();
    line.starts_with("###")
        || line.starts_with("Plan item:")
        || [
            "Files:",
            "Notes:",
            "Task metadata:",
            "Title:",
            "Why:",
            "How:",
            "Priority:",
            "Done criteria",
            "Done when",
            "Invariant",
            "Plan-Item:",
            "Writer:",
            "Changelog:",
        ]
        .iter()
        .any(|h| line.starts_with(h))
}

/// Every section opened by a line starting with one of `labels` (case-insensitive): the
/// text after the label on that line, then each following line up to the next heading.
/// Multi-line criteria of the feeder layout (`Done criteria:` alone on its line) and the
/// one-line batch layout both come out whole, one entry per item.
fn sections(brief: &str, labels: &[&str]) -> Vec<String> {
    let mut found = Vec::new();
    let mut open: Option<String> = None;
    for line in brief.lines() {
        let lower = line.trim_start().to_ascii_lowercase();
        if let Some(label) = labels.iter().find(|l| lower.starts_with(*l)) {
            found.extend(open.take());
            let rest = line.trim_start()[label.len()..].trim_start_matches([':', ' ']);
            open = Some(rest.to_owned());
        } else if heading(line) {
            found.extend(open.take());
        } else if let Some(text) = open.as_mut() {
            text.push('\n');
            text.push_str(line.trim_end());
        }
    }
    found.extend(open);
    found
        .into_iter()
        .map(|text| text.trim().to_owned())
        .filter(|text| !text.is_empty())
        .collect()
}

fn joined(sections: Vec<String>) -> Option<String> {
    // Whole, never truncated: a later item's criteria must not be dropped.
    let text = sections.join("\n---\n");
    (!text.is_empty()).then_some(text)
}

/// The plan-item keys named by the writer brief.
pub(super) fn item_keys(brief: &str) -> Vec<String> {
    let found: BTreeSet<String> = regex::Regex::new(r"plangraph:[A-Za-z0-9_-]+")
        .map(|re| re.find_iter(brief).map(|m| m.as_str().to_owned()).collect())
        .unwrap_or_default();
    found.into_iter().collect()
}

/// The done criteria of every item of the brief, whole.
pub(super) fn done_criteria(brief: &str) -> Option<String> {
    joined(sections(brief, &["done criteria", "done when"]))
}

/// The invariants the item names (an `Invariants:` section, else its `How:` text).
pub(super) fn invariants(brief: &str) -> Option<String> {
    joined(sections(brief, &["invariants:", "invariant:", "how:"]))
}

/// A public symbol the diff touches: its name and whether it declares a type (Rust struct,
/// enum, trait or type alias; Python class), the only kind a path `Name::item` can use.
#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub(super) struct Symbol {
    pub(super) name: String,
    pub(super) type_like: bool,
}

/// A definition line: its indent, symbol and whether it is public (Rust `pub ...`, Python
/// names without a leading underscore).
fn definition(line: &str, python: bool) -> Option<(usize, Symbol, bool)> {
    let indent = line.len() - line.trim_start().len();
    let rest = line.trim_start();
    if python {
        let rest = rest.strip_prefix("async ").unwrap_or(rest);
        let (type_like, name) = match rest.strip_prefix("def ") {
            Some(name) => (false, name),
            None => (true, rest.strip_prefix("class ")?),
        };
        let name: String = name
            .chars()
            .take_while(|c| c.is_alphanumeric() || *c == '_')
            .collect();
        let public = !name.starts_with('_');
        return (!name.is_empty()).then_some((indent, Symbol { name, type_like }, public));
    }
    let (public, mut rest) = match rest.strip_prefix("pub") {
        Some(after) if after.starts_with([' ', '(']) => {
            let after = after
                .strip_prefix(|c| c == '(')
                .map_or(after, |a| a.split_once(')').map_or(a, |(_, r)| r));
            (true, after.trim_start())
        }
        _ => (false, rest),
    };
    for word in ["async ", "const ", "unsafe "] {
        if let Some(r) = rest.strip_prefix(word) {
            if !r.starts_with(|c: char| c.is_ascii_uppercase()) || word != "const " {
                rest = r;
            }
        }
    }
    let (keyword, name) = ["fn ", "struct ", "enum ", "trait ", "const ", "type "]
        .iter()
        .find_map(|kw| Some((*kw, rest.strip_prefix(kw)?)))?;
    let name: String = name
        .chars()
        .take_while(|c| c.is_alphanumeric() || *c == '_')
        .collect();
    let type_like = ["struct ", "enum ", "trait ", "type "].contains(&keyword);
    (!name.is_empty()).then_some((indent, Symbol { name, type_like }, public))
}

/// Whether the body of a definition at `indent` is still open at line `at`: no later
/// non-blank line between them returns to that indent, other than a signature continuation
/// (`)`, Rust `where`, `{`) that Rust and Python wrap at the definition's own indent.
fn scope_open(between: &[&str], indent: usize, python: bool) -> bool {
    between.iter().all(|line| {
        let text = line.trim_start();
        let continuation = text.starts_with([')', '{', ']']) || (!python && is_where(text));
        text.is_empty() || line.len() - text.len() > indent || continuation
    })
}

/// Whether `text` continues or closes a definition at its own indent (`)`, `}`, `]`, `{`,
/// Rust `where`).
fn closes_or_continues(text: &str, python: bool) -> bool {
    text.starts_with([')', '{', '}', ']']) || (!python && is_where(text))
}

/// The Rust `where` keyword at the start of `text`, not an identifier that begins with it.
fn is_where(text: &str) -> bool {
    text.strip_prefix("where")
        .is_some_and(|rest| !rest.starts_with(|c: char| c.is_alphanumeric() || c == '_'))
}

/// `line` without its comments: a trailing `//` (Rust) or `#` (Python) and everything after it,
/// and every closed `/* ... */` (Rust) with the code around it kept; an unterminated `/*` runs
/// to the line end. A marker inside
/// a literal is text: Python `'`, `"` and triple-quoted strings, Rust strings, raw strings
/// (`r"..."`, `r#"..."#`, no escapes) and char literals (`'"'`, `'\\''`; a lifetime is none).
fn code_part(line: &str, python: bool) -> String {
    let chars: Vec<(usize, char)> = line.char_indices().collect();
    let at = |i: usize| chars.get(i).map(|(_, c)| *c);
    let ident = |i: Option<char>| i.is_some_and(|c| c.is_alphanumeric() || c == '_');
    let (mut i, mut kept, mut out) = (0, 0, String::new());
    while i < chars.len() {
        let c = chars[i].1;
        i = match c {
            '#' if python => break,
            '/' if !python && at(i + 1) == Some('/') => break,
            '/' if !python && at(i + 1) == Some('*') => {
                out.push_str(&line[kept..chars[i].0]);
                let close = block_comment_end(&chars, i + 2);
                let Some(close) = close else {
                    return out;
                };
                kept = chars.get(close + 2).map_or(line.len(), |(byte, _)| *byte);
                close + 2
            }
            '"' | '\'' if python && at(i + 1) == Some(c) && at(i + 2) == Some(c) => {
                skip_literal(&chars, i + 3, c, 3)
            }
            '"' | '\'' if python => skip_literal(&chars, i + 1, c, 1),
            '"' => skip_literal(&chars, i + 1, '"', 1),
            'r' if !python && (!ident(i.checked_sub(1).and_then(at)) || is_byte_raw(&chars, i)) => {
                let hashes = (i + 1..chars.len())
                    .take_while(|&k| chars[k].1 == '#')
                    .count();
                if at(i + 1 + hashes) == Some('"') {
                    skip_raw(&chars, i + 2 + hashes, hashes)
                } else {
                    i + 1
                }
            }
            '\'' if at(i + 1) == Some('\\') => skip_literal(&chars, i + 1, '\'', 1),
            '\'' if at(i + 2) == Some('\'') => i + 3,
            _ => i + 1,
        };
    }
    let end = chars.get(i).map_or(line.len(), |(byte, _)| *byte);
    out.push_str(&line[kept..end.max(kept)]);
    out
}

/// Index of the `*/` that closes a Rust block comment whose body starts at `from` (block
/// comments nest); none when the line ends first.
fn block_comment_end(chars: &[(usize, char)], from: usize) -> Option<usize> {
    let pair = |k: usize, a: char, b: char| {
        chars.get(k).is_some_and(|(_, c)| *c == a) && chars.get(k + 1).is_some_and(|(_, c)| *c == b)
    };
    let (mut depth, mut k) = (1, from);
    while k < chars.len() {
        if pair(k, '/', '*') {
            depth += 1;
            k += 2;
        } else if pair(k, '*', '/') {
            depth -= 1;
            if depth == 0 {
                return Some(k);
            }
            k += 2;
        } else {
            k += 1;
        }
    }
    None
}

/// `br"` / `br#"`: an `r` after a `b` that itself starts a token.
fn is_byte_raw(chars: &[(usize, char)], at: usize) -> bool {
    at >= 1
        && chars[at - 1].1 == 'b'
        && (at < 2 || !(chars[at - 2].1.is_alphanumeric() || chars[at - 2].1 == '_'))
}

/// Index just past the end of a literal opened with `quote` repeated `count` times, whose body
/// starts at `from`; a backslash escapes the next char, and the line end closes it.
fn skip_literal(chars: &[(usize, char)], from: usize, quote: char, count: usize) -> usize {
    let mut i = from;
    while i < chars.len() {
        if chars[i].1 == '\\' {
            i += 2;
        } else if chars[i..]
            .iter()
            .take(count)
            .filter(|(_, c)| *c == quote)
            .count()
            == count
        {
            return i + count;
        } else {
            i += 1;
        }
    }
    chars.len()
}

/// Index just past the end of a raw string whose body starts at `from`: a `"` followed by
/// `hashes` `#`; the line end closes it.
fn skip_raw(chars: &[(usize, char)], from: usize, hashes: usize) -> usize {
    (from..chars.len())
        .find(|&i| {
            chars[i].1 == '"'
                && (1..=hashes).all(|k| chars.get(i + k).is_some_and(|(_, c)| *c == '#'))
        })
        .map_or(chars.len(), |i| i + 1 + hashes)
}

/// The indent that places line `index` in a scope: its own indent. A blank line takes the
/// indent of the line before it: one level deeper when that line opens a block (ends in `{`,
/// or in `:` for Python), else the block around it; lowered to the next line's indent when
/// that line leaves the block without closing it. None before the first or after the last
/// non-blank line.
fn scope_indent(lines: &[&str], index: usize, python: bool) -> Option<usize> {
    let indent = |line: &str| line.len() - line.trim_start().len();
    if !lines[index].trim().is_empty() {
        return Some(indent(lines[index]));
    }
    let before = lines[..index].iter().rev().find(|l| !l.trim().is_empty())?;
    let after = lines[index..].iter().find(|l| !l.trim().is_empty())?;
    let code = code_part(before, python);
    let code = code.trim_end();
    let opens = code.ends_with('{') || (python && code.ends_with(':'));
    if opens {
        return Some(indent(before) + 1);
    }
    let own = indent(before);
    let leaves = !closes_or_continues(after.trim_start(), python) && indent(after) < own;
    Some(if leaves { indent(after) } else { own })
}

/// The nearest enclosing public definition of line `at` (1-based) of `source`: only
/// definitions whose body still contains the line count. A line at the definition's own
/// indent is outside its body unless it wraps the signature or closes the body, so a line
/// after a closed `impl`, after a one-line function or a module-level assignment belongs to
/// none of them.
fn enclosing_public(source: &str, at: usize, python: bool) -> Option<Symbol> {
    let lines: Vec<&str> = source.lines().collect();
    let line = *lines.get(at.checked_sub(1)?)?;
    if let Some((_, symbol, true)) = definition(line, python) {
        return Some(symbol);
    }
    let own = scope_indent(&lines, at - 1, python)?;
    let closing = closes_or_continues(line.trim_start(), python);
    let mut scope = own + 1;
    for (index, earlier) in lines[..at - 1].iter().enumerate().rev() {
        let Some((indent, symbol, public)) = definition(earlier, python) else {
            continue;
        };
        let inside = indent < own || (closing && indent == own);
        if inside && indent < scope && scope_open(&lines[index + 1..at - 1], indent, python) {
            if public {
                return Some(symbol);
            }
            scope = indent;
        }
    }
    None
}

/// Public symbols a unified diff (`-U0`) touches: the nearest enclosing public Rust or
/// Python definition of every changed line of a `.rs`/`.py` file, so a changed body counts
/// as well as an added declaration. `read(path)` returns that file's text at the reviewed head.
pub(super) fn changed_symbols(diff: &str, read: &dyn Fn(&str) -> Option<String>) -> Vec<Symbol> {
    let hunk = regex::Regex::new(r"^@@ -\S+ \+(\d+)(?:,(\d+))? @@");
    let mut found = BTreeMap::new();
    let (mut path, mut source): (Option<String>, Option<String>) = (None, None);
    for line in diff.lines() {
        if let Some(file) = line.strip_prefix("+++ b/") {
            let supported = file.ends_with(".rs") || file.ends_with(".py");
            source = supported.then(|| read(file)).flatten();
            path = supported.then(|| file.to_owned());
        } else if let (Some(caps), Some(text), Some(file)) = (
            hunk.as_ref().ok().and_then(|re| re.captures(line)),
            source.as_deref(),
            path.as_deref(),
        ) {
            let start: usize = caps[1].parse().unwrap_or(0);
            let count: usize = caps.get(2).map_or(1, |m| m.as_str().parse().unwrap_or(1));
            for at in start..start + count.max(1) {
                if let Some(symbol) = enclosing_public(text, at, file.ends_with(".py")) {
                    // One entry per name; a name that also declares a type keeps the type kind.
                    *found.entry(symbol.name).or_insert(false) |= symbol.type_like;
                }
            }
        }
    }
    found
        .into_iter()
        .map(|(name, type_like)| Symbol { name, type_like })
        .collect()
}

/// Whether a `git grep -n` line is a use of `symbol` worth naming: a `.rs`/`.py` code line
/// that is neither a comment nor the symbol's own definition and holds the symbol as code
/// (outside a string literal) in call or path position: `symbol(`, `symbol::<`, `symbol::`
/// (only a type; after a function or constant the name is a module path, not a use),
/// `symbol {`, `symbol<` or `symbol!(`; `mod symbol;` is a declaration, not a use.
fn is_caller(symbol: &Symbol, line: &str) -> bool {
    let type_like = symbol.type_like;
    let symbol = symbol.name.as_str();
    let mut parts = line.splitn(3, ':');
    let (Some(path), Some(_), Some(code)) = (parts.next(), parts.next(), parts.next()) else {
        return false;
    };
    let code = code.trim_start();
    let boundary = |c: Option<char>| !c.is_some_and(|c| c.is_alphanumeric() || c == '_');
    let declared = [
        "fn ", "def ", "class ", "struct ", "enum ", "trait ", "const ", "type ", "mod ",
    ]
    .iter()
    .any(|kw| {
        code.split(kw).skip(1).any(|after| {
            after
                .strip_prefix(symbol)
                .is_some_and(|rest| boundary(rest.chars().next()))
        })
    });
    let used = code.match_indices(symbol).any(|(at, _)| {
        let before = code[..at].chars().next_back();
        let rest = &code[at + symbol.len()..];
        let in_string = code[..at].matches('"').count() % 2 == 1;
        boundary(before)
            && boundary(rest.chars().next())
            && !in_string
            && (rest.starts_with(['(', '<', '{'])
                || rest.starts_with("::<")
                || (rest.starts_with("::") && type_like)
                || rest.starts_with(" {")
                || rest.starts_with("!("))
    });
    (path.ends_with(".rs") || path.ends_with(".py"))
        && !code.starts_with("//")
        && !code.starts_with('#')
        && !code.starts_with('*')
        && !declared
        && used
}

/// `symbol: file:line, ...` for the first few real uses of each symbol, from `git grep -n -w`.
pub(super) fn callers_line(symbol: &Symbol, grep: &str) -> Option<String> {
    let hits: Vec<String> = grep
        .lines()
        .filter(|line| is_caller(symbol, line))
        .filter_map(|line| {
            let mut parts = line.splitn(3, ':');
            Some(format!("{}:{}", parts.next()?, parts.next()?))
        })
        .take(MAX_CALLERS_PER_SYMBOL)
        .collect();
    (!hits.is_empty()).then(|| format!("{}: {}", symbol.name, hits.join(", ")))
}

#[cfg(test)]
#[path = "native_writer_context_tests.rs"]
mod tests;
