//! What `land-train cut` and `cut-check` refuse before any pick: a commit that edits a gate path or carries a
//! `Gate-Edit:` line. No source of a grant is accepted yet (the producer slice is `GRANT_PRODUCER`), so there is
//! nothing to check the provenance of; the pinned commit gate stays the backstop after the pick.
use super::git;
use super::go_verify::OPERATOR_CLASS;
use std::path::Path;

/// The slice that will produce the pre-cut grant record; named in the refusal of `--grants`.
pub(super) const GRANT_PRODUCER: &str = "LAND-CUT-GRANT-RECORD-PRODUCER";

/// The refusal of a flag that would hand the cutter a grant.
pub(super) fn grants_refused(cmd: &str, flag: &str) -> String {
    format!("{cmd}: --{flag}: grants are not accepted yet: {GRANT_PRODUCER}")
}

/// The lines of `text` as the pinned gate sees them: Python's `str.splitlines()` breaks at more than `\n`
/// (`\r`, `\r\n`, VT, FF, the C0 separators 1c-1e, NEL, LS and PS), and a `Gate-Edit:` trailer after one of them
/// is a trailer to the gate.
pub(super) fn py_lines(text: &str) -> Vec<&str> {
    let mut lines = Vec::new();
    let mut start = 0;
    let mut chars = text.char_indices().peekable();
    while let Some((i, c)) = chars.next() {
        if !matches!(
            c,
            '\n' | '\r'
                | '\x0b'
                | '\x0c'
                | '\x1c'
                | '\x1d'
                | '\x1e'
                | '\u{85}'
                | '\u{2028}'
                | '\u{2029}'
        ) {
            continue;
        }
        lines.push(&text[start..i]);
        start = i + c.len_utf8();
        if c == '\r' && chars.peek().is_some_and(|(_, n)| *n == '\n') {
            chars.next();
            start += 1;
        }
    }
    if start < text.len() {
        lines.push(&text[start..]);
    }
    lines
}

/// The paths only an operator-granted `Gate-Edit` may change, as the router's GO verifier lists them. The
/// pinned gate has the authoritative list (with wildcard suffixes) and runs after the pick as the backstop.
fn operator_class(path: &str) -> bool {
    OPERATOR_CLASS.iter().any(|p| match p.strip_suffix('/') {
        Some(_) => path.starts_with(p),
        None => {
            path == *p || path.starts_with(&format!("{p}/")) || path.ends_with(&format!("/{p}"))
        }
    })
}

/// Refuses `commit` when its message has a `Gate-Edit:` line anywhere (also one git's trailer parser misses
/// and one cutting would expose) or when it changes an operator-class path. Paths are read NUL-delimited and,
/// for a merge, against every parent.
pub(super) fn refuse_gate_edit(repo: &Path, commit: &str, message: &str) -> Result<(), String> {
    for line in py_lines(message) {
        if let Some((key, _)) = line.trim().split_once(':') {
            if key.trim().eq_ignore_ascii_case("gate-edit") {
                return Err(format!(
                    "cut: {commit} carries a Gate-Edit line; grants are not accepted yet: {GRANT_PRODUCER}"
                ));
            }
        }
    }
    let raw = git::bytes(
        repo,
        &[
            "diff-tree",
            "--no-commit-id",
            "--name-only",
            "-r",
            "-z",
            "-m",
            "--root",
            commit,
        ],
    )?;
    for path in raw.split(|b| *b == 0).filter(|p| !p.is_empty()) {
        // Lossy only for matching: the patterns are ASCII, so a replaced byte cannot make a path match.
        let path = String::from_utf8_lossy(path);
        if operator_class(&path) {
            return Err(format!(
                "cut: {commit} edits the gate path {path}; grants are not accepted yet: {GRANT_PRODUCER}"
            ));
        }
    }
    Ok(())
}
