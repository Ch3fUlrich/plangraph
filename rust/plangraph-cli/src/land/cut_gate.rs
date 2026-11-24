//! What `land-train cut` and `cut-check` refuse before any pick: a commit that edits a gate path or carries a
//! `Gate-Edit:` line. No source of a grant is accepted yet (the producer slice is `GRANT_PRODUCER`), so there is
//! nothing to check the provenance of; the pinned commit gate stays the backstop after the pick.
use super::git;
use super::go_verify::OPERATOR_CLASS;
use std::path::Path;
use std::process::Command;

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

/// `GATE_PATHS` of the pinned gate script, read from the script itself so that the one list stays in the gate.
pub(super) fn pinned_gate_paths(
    gate: &Path,
    python: &dyn Fn() -> Command,
) -> Result<Vec<String>, String> {
    const READ: &str = "import importlib.util, json, sys\n\
        spec = importlib.util.spec_from_file_location('pinned_gate', sys.argv[1])\n\
        mod = importlib.util.module_from_spec(spec)\n\
        sys.modules['pinned_gate'] = mod\n\
        spec.loader.exec_module(mod)\n\
        print(json.dumps(list(mod.GATE_PATHS)))\n";
    let gate =
        std::path::absolute(gate).map_err(|e| format!("gate path {}: {e}", gate.display()))?;
    let out = python()
        .args(["-B", "-c", READ])
        .arg(&gate)
        .output()
        .map_err(|e| format!("commit gate unavailable: {e}"))?;
    if !out.status.success() {
        return Err(format!(
            "commit gate {} gives no GATE_PATHS: {}",
            gate.display(),
            String::from_utf8_lossy(&out.stderr).trim()
        ));
    }
    let patterns: Vec<String> = serde_json::from_slice(&out.stdout)
        .map_err(|e| format!("commit gate {} GATE_PATHS: {e}", gate.display()))?;
    if patterns.iter().any(|p| p.contains('[')) {
        return Err("a GATE_PATHS pattern with a character class is not understood".into());
    }
    Ok(patterns)
}

/// Python's `fnmatch.fnmatchcase` for the patterns the gate uses: `*` (also across `/`) and `?`.
fn fnmatch(pattern: &[u8], text: &[u8]) -> bool {
    match pattern.split_first() {
        None => text.is_empty(),
        Some((b'*', rest)) => (0..=text.len()).any(|i| fnmatch(rest, &text[i..])),
        Some((b'?', rest)) => !text.is_empty() && fnmatch(rest, &text[1..]),
        Some((c, rest)) => text.first() == Some(c) && fnmatch(rest, &text[1..]),
    }
}

/// Refuses `commit` when its message has a `Gate-Edit:` line anywhere (also one git's trailer parser misses
/// and one cutting would expose) or when it changes a path of the pinned gate's `GATE_PATHS` (`patterns`) or
/// an operator-class path. Paths are read NUL-delimited and, for a merge, against every parent.
pub(super) fn refuse_gate_edit(
    repo: &Path,
    commit: &str,
    message: &str,
    patterns: &[String],
) -> Result<(), String> {
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
        // Matched as raw bytes by the gate's patterns; lossy only for the operator-class list and the message.
        let shown = String::from_utf8_lossy(path);
        if operator_class(&shown) || patterns.iter().any(|p| fnmatch(p.as_bytes(), path)) {
            return Err(format!(
                "cut: {commit} edits the gate path {shown}; grants are not accepted yet: {GRANT_PRODUCER}"
            ));
        }
    }
    Ok(())
}
