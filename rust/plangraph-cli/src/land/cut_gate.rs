//! What `land-train cut` and `cut-check` refuse before any pick: a commit that edits a gate path or carries a
//! `Gate-Edit:` line. No source of a grant is accepted yet (the producer slice is `GRANT_PRODUCER`), so there is
//! nothing to check the provenance of. The gate paths are the pinned gate script's own `GATE_PATHS`; the same
//! gate then runs on the picked range as the backstop.
use super::git;
use std::io::Write;
use std::path::Path;
use std::process::{Command, Stdio};

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

/// The pinned gate script and the interpreter that runs it. The gate owns the rule for its protected paths, so
/// the preflight asks the gate's own `GATE_PATHS` and Python's own `fnmatch.fnmatchcase` (the call the gate
/// makes), and keeps no second matcher.
pub(super) struct PinnedGate<'a> {
    pub gate: &'a Path,
    pub python: &'a dyn Fn() -> Command,
}

impl PinnedGate<'_> {
    /// The paths among `paths` (raw git bytes, decoded like the gate does) that match its `GATE_PATHS`.
    /// Python runs isolated (`-I`): neither the caller's directory nor `PYTHON*` variables can put code in
    /// front of the gate's, and any failure to answer is a refusal.
    pub(super) fn matching(&self, paths: &[&[u8]]) -> Result<Vec<String>, String> {
        const READ: &str = "import fnmatch, importlib.util, json, sys\n\
            spec = importlib.util.spec_from_file_location('pinned_gate', sys.argv[1])\n\
            mod = importlib.util.module_from_spec(spec)\n\
            sys.modules['pinned_gate'] = mod\n\
            spec.loader.exec_module(mod)\n\
            raw = sys.stdin.buffer.read().split(b'\\0')\n\
            paths = [p.decode('utf-8', errors='replace') for p in raw if p]\n\
            hits = [p for p in paths if any(fnmatch.fnmatchcase(p, g) for g in mod.GATE_PATHS)]\n\
            print(json.dumps(hits))\n";
        let gate = std::path::absolute(self.gate)
            .map_err(|e| format!("gate path {}: {e}", self.gate.display()))?;
        let mut child = (self.python)()
            .args(["-I", "-B", "-c", READ])
            .arg(&gate)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .map_err(|e| format!("commit gate unavailable: {e}"))?;
        let mut input = Vec::new();
        for p in paths {
            input.extend_from_slice(p);
            input.push(0);
        }
        child
            .stdin
            .take()
            .ok_or("gate loader has no stdin")?
            .write_all(&input)
            .map_err(|e| e.to_string())?;
        let out = child.wait_with_output().map_err(|e| e.to_string())?;
        if !out.status.success() {
            return Err(format!(
                "commit gate {} gives no answer for GATE_PATHS: {}",
                gate.display(),
                String::from_utf8_lossy(&out.stderr).trim()
            ));
        }
        serde_json::from_slice(&out.stdout)
            .map_err(|e| format!("commit gate {} GATE_PATHS answer: {e}", gate.display()))
    }
}

/// Refuses `commit` when its message has a `Gate-Edit:` line anywhere (also one git's trailer parser misses
/// and one cutting would expose) or when it changes a path of the pinned gate's `GATE_PATHS`. Paths are read
/// NUL-delimited and, for a merge, against every parent.
pub(super) fn refuse_gate_edit(
    repo: &Path,
    commit: &str,
    message: &str,
    pinned: &PinnedGate,
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
    let paths: Vec<&[u8]> = raw.split(|b| *b == 0).filter(|p| !p.is_empty()).collect();
    let hit = pinned.matching(&paths)?.into_iter().next();
    match hit {
        Some(path) => Err(format!(
            "cut: {commit} edits the gate path {path}; grants are not accepted yet: {GRANT_PRODUCER}"
        )),
        None => Ok(()),
    }
}
