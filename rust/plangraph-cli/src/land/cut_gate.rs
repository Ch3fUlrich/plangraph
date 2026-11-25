//! `land-train cut-check`: the read-only check of a hand-cut range `base..head`. One pinned setup serves every
//! step: the revisions are resolved once to full commit ids, the commit gate script comes from the base object,
//! and a private scratch checkout of the head carries the Git scanning and both Python invocations (the loader
//! of the gate's own rules and the gate itself). Both Python runs are isolated and share one sanitized Git
//! environment. A commit is refused for a `(cherry picked from commit ...)` line, a `Gate-Edit:` line anywhere
//! in its message, or a protected path: the union of the pinned gate's `GATE_PATHS` (its own Python
//! `fnmatch.fnmatchcase`) and the GO verifier's operator-class paths. No source of a grant is accepted yet.
use super::git;
use super::go_verify::operator_class;
use std::collections::BTreeMap;
use std::ffi::OsString;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};

/// The slice that will produce the pre-cut grant record; named in the refusal of `--grants`.
pub(super) const GRANT_PRODUCER: &str = "LAND-CUT-GRANT-RECORD-PRODUCER";

/// The refusal of a flag that would hand the checker a grant.
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

/// A `(cherry picked from commit ...)` line is what `cherry-pick -x` leaves; inside the trailer paragraph it
/// ends the block.
pub(super) fn is_picked_line(line: &str) -> bool {
    line.starts_with("(cherry picked from commit ")
}

/// The full id of the commit `rev` names; a revision that starts with `-` is never passed to git.
pub(super) fn resolve(repo: &Path, rev: &str) -> Result<String, String> {
    if rev.is_empty() || rev.starts_with('-') {
        return Err(format!("cut-check: {rev:?} is not a commit"));
    }
    git::out(
        repo,
        &["rev-parse", "--verify", &format!("{rev}^{{commit}}")],
    )
    .map_err(|e| format!("cut-check: {rev} is not a commit: {e}"))
}

/// The environment of a Python run of the pinned gate: no `GIT_*` variable of the caller or of the command
/// (no repository override, no replacement refs), no user or system configuration, no host attributes
/// (the checkout's tracked ones stay), and no CI event file for the gate to read.
fn pin_env(cmd: &mut Command) {
    let mut keys: Vec<OsString> = std::env::vars_os().map(|(k, _)| k).collect();
    keys.extend(cmd.get_envs().map(|(k, _)| k.to_owned()));
    for k in keys {
        if k.to_string_lossy().starts_with("GIT_") || k == "GITHUB_EVENT_PATH" {
            cmd.env_remove(k);
        }
    }
    cmd.env("GIT_CONFIG_GLOBAL", "/dev/null")
        .env("GIT_CONFIG_SYSTEM", "/dev/null")
        .env("GIT_CONFIG_NOSYSTEM", "1")
        .env("GIT_NO_REPLACE_OBJECTS", "1")
        .env("GIT_TERMINAL_PROMPT", "0")
        .env("GIT_ATTR_NOSYSTEM", "1")
        .env("GIT_CONFIG_COUNT", "1")
        .env("GIT_CONFIG_KEY_0", "core.attributesFile")
        .env("GIT_CONFIG_VALUE_0", "/dev/null");
}

/// What the loader reports for one commit: the paths it judged and those among them the gate protects.
pub(super) struct Matched {
    pub paths: Vec<String>,
    pub hits: Vec<String>,
}

#[derive(serde::Deserialize)]
struct Answer {
    paths: Vec<String>,
    hits: Vec<String>,
}

/// The pinned gate script, the scratch checkout it works in and the interpreter that runs it. The gate owns the
/// rule for its protected paths, so the checker asks the gate's own `GATE_PATHS` and Python's own
/// `fnmatch.fnmatchcase` (the call the gate makes) and keeps no second matcher.
pub(super) struct PinnedGate<'a> {
    pub gate: &'a Path,
    pub checkout: &'a Path,
    pub python: &'a dyn Fn() -> Command,
}

impl PinnedGate<'_> {
    /// An isolated Python (`-I`: neither the working directory nor `PYTHON*` variables put code in front of the
    /// gate's) in the scratch checkout under the pinned Git environment.
    fn command(&self) -> Command {
        let mut cmd = (self.python)();
        pin_env(&mut cmd);
        cmd.args(["-I", "-B"]).current_dir(self.checkout);
        cmd
    }

    /// The protected paths of one commit. `paths` are the raw git bytes of everything the commit changes
    /// (against every parent for a merge), decoded like the gate does. For a two-parent merge (`merge` names
    /// it) the pinned gate's own `_merge_edit_evidence` decides what the merge edited itself when it has replay
    /// evidence, so a clean merge of a protected change that came in from the base is not an edit; without
    /// replay evidence the all-parent `paths` stand. A gate that cannot answer is a refusal.
    pub(super) fn matching(&self, paths: &[&[u8]], merge: Option<&str>) -> Result<Matched, String> {
        const READ: &str = "import fnmatch, importlib.util, json, sys\n\
            spec = importlib.util.spec_from_file_location('pinned_gate', sys.argv[1])\n\
            mod = importlib.util.module_from_spec(spec)\n\
            sys.modules['pinned_gate'] = mod\n\
            spec.loader.exec_module(mod)\n\
            raw = sys.stdin.buffer.read().split(b'\\0')\n\
            paths = [p.decode('utf-8', errors='replace') for p in raw if p]\n\
            if sys.argv[2] and hasattr(mod, '_merge_edit_evidence'):\n\
            \x20   evidence = mod._merge_edit_evidence(sys.argv[2], sys.argv[3])\n\
            \x20   if evidence[2] is not None:\n\
            \x20       paths = list(evidence[0])\n\
            hits = [p for p in paths if any(fnmatch.fnmatchcase(p, g) for g in mod.GATE_PATHS)]\n\
            print(json.dumps({'paths': paths, 'hits': hits}))\n";
        let mut child = self
            .command()
            .args(["-c", READ])
            .arg(self.gate)
            .arg(merge.unwrap_or(""))
            .arg(self.checkout)
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
                self.gate.display(),
                String::from_utf8_lossy(&out.stderr).trim()
            ));
        }
        let a: Answer = serde_json::from_slice(&out.stdout)
            .map_err(|e| format!("commit gate {} GATE_PATHS answer: {e}", self.gate.display()))?;
        Ok(Matched {
            paths: a.paths,
            hits: a.hits,
        })
    }
}

/// Refuses `commit` (a commit of the scratch checkout) for a cherry-pick marker (unless `allow_picked`: a
/// source commit may carry one for the cutter to remove), a `Gate-Edit:` line anywhere in its message (also one
/// git's trailer parser misses) or a protected path. Paths are read NUL-delimited with renames split into a
/// delete and an add and, for a merge, against every parent. The cutter's source and output checks call this same
/// function.
pub(super) fn scan_commit(
    pinned: &PinnedGate,
    commit: &str,
    allow_picked: bool,
) -> Result<(), String> {
    let repo = pinned.checkout;
    let message = git::out(repo, &["log", "-1", "--format=%B", commit])?;
    let lines = py_lines(&message);
    if !allow_picked && lines.iter().copied().any(is_picked_line) {
        return Err(format!(
            "{commit}: a `(cherry picked from commit ...)` line ends the trailer block; recut by hand without `-x`"
        ));
    }
    for line in lines {
        if let Some((key, _)) = line.trim().split_once(':') {
            if key.trim().eq_ignore_ascii_case("gate-edit") {
                return Err(format!(
                    "{commit} carries a Gate-Edit line; grants are not accepted yet: {GRANT_PRODUCER}"
                ));
            }
        }
    }
    let parents = git::out(repo, &["rev-list", "--parents", "-n", "1", commit])?
        .split_whitespace()
        .count()
        - 1;
    let mut args = vec![
        "diff-tree",
        "--no-commit-id",
        "--name-only",
        "-r",
        "-z",
        "--no-renames",
        "--root",
    ];
    if parents > 1 {
        args.push("-m");
    }
    args.push(commit);
    let raw = git::bytes(repo, &args)?;
    let changed: Vec<&[u8]> = raw.split(|b| *b == 0).filter(|p| !p.is_empty()).collect();
    let judged = pinned.matching(&changed, (parents == 2).then_some(commit))?;
    if let Some(path) = judged.hits.first() {
        return Err(format!(
            "{commit} edits the gate path {path}; grants are not accepted yet: {GRANT_PRODUCER}"
        ));
    }
    if let Some(path) = judged.paths.iter().find(|p| operator_class(p)) {
        return Err(format!(
            "{commit} edits the operator-class path {path}; grants are not accepted yet: {GRANT_PRODUCER}"
        ));
    }
    Ok(())
}

/// The check of `base..head` for a branch cut by hand: every commit is scanned, then the pinned commit gate
/// (Writer trailer, gate files) runs on the same range, given as full commit ids. The caller's checkout and refs
/// are not touched.
pub fn check(
    repo: &Path,
    base: &str,
    head: &str,
    python: &dyn Fn() -> Command,
) -> Result<(), String> {
    let (base, head) = (resolve(repo, base)?, resolve(repo, head)?);
    let checkout = git::Checkout::new(repo, &head)?;
    // The gate script is the base object's own, written outside the checkout.
    let scratch = tempfile::tempdir().map_err(|e| e.to_string())?;
    let gate = scratch.path().join("ci_commit_gate.py");
    std::fs::write(
        &gate,
        git::bytes(
            checkout.path(),
            &["show", &format!("{base}:tools/ci_commit_gate.py")],
        )?,
    )
    .map_err(|e| e.to_string())?;
    let pinned = PinnedGate {
        gate: &gate,
        checkout: checkout.path(),
        python,
    };
    for c in git::commits(checkout.path(), &format!("{base}..{head}"))? {
        scan_commit(&pinned, &c, false)?;
    }
    let out = pinned
        .command()
        .arg(&gate)
        .args(["check", "--base", &base, "--head", &head])
        .output()
        .map_err(|e| format!("commit gate unavailable: {e}"))?;
    if out.status.success() {
        Ok(())
    } else {
        Err(format!(
            "commit gate refused {base}..{head}:\n{}{}",
            String::from_utf8_lossy(&out.stdout),
            String::from_utf8_lossy(&out.stderr)
        ))
    }
}

/// `cut-check --repo DIR --base REV --head REV`. The commit gate is `tools/ci_commit_gate.py` of the base
/// commit, as CI reads it; no flag names another. `--grants` and `--gate-edits` are refused: no grant is accepted.
pub fn run_cli(
    flags: &BTreeMap<String, String>,
    python: &dyn Fn() -> Command,
) -> Result<Vec<String>, String> {
    const CMD: &str = "cut-check";
    let need = |k: &str| {
        flags
            .get(k)
            .cloned()
            .ok_or_else(|| format!("{CMD}: missing --{k}"))
    };
    for flag in ["grants", "gate-edits"] {
        if flags.contains_key(flag) {
            return Err(grants_refused(CMD, flag));
        }
    }
    if flags.contains_key("gate") {
        return Err(format!(
            "{CMD}: --gate is not accepted; the gate is read from the base commit"
        ));
    }
    check(
        &PathBuf::from(need("repo")?),
        &need("base")?,
        &need("head")?,
        python,
    )?;
    Ok(vec!["commit gate accepts the range".into()])
}
