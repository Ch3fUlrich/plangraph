//! `land-train cut`: cuts a slice or landing branch from source commits without breaking the trailer block.
//! Commits are picked without `-x`; the full source id goes into a `Picked-From:` trailer inside the block and
//! the commit gate of the pinned base must accept the result before the branch exists. A range that edits a
//! gate path or carries a `Gate-Edit:` line is refused before the first pick: no grant is accepted yet.
//! `check` is the same test for a branch cut by hand.
use super::cut_gate::{grants_refused, pinned_gate_paths, py_lines, refuse_gate_edit};
use super::git;
use std::collections::BTreeMap;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};

pub struct Cut<'a> {
    pub repo: &'a Path,
    pub base: &'a str,
    /// The branch to create; it must not exist.
    pub branch: &'a str,
    /// Source commits (any revision that names a commit), oldest first.
    pub commits: &'a [String],
    /// The commit gate script. The CLI writes the one of the pinned base here; tests wire their own.
    pub gate: &'a Path,
}

/// A `(cherry picked from commit ...)` line is what `cherry-pick -x` leaves; inside the trailer paragraph
/// it ends the block, so the cutter writes `Picked-From:` instead and drops the line.
fn is_picked_line(line: &str) -> bool {
    line.starts_with("(cherry picked from commit ")
}

/// The message with the `-x` line removed and the trailers added inside its trailer block.
fn with_trailers(message: &str, trailers: &[String]) -> Result<String, String> {
    let kept: Vec<&str> = py_lines(message)
        .into_iter()
        .filter(|l| !is_picked_line(l))
        .collect();
    let mut text = kept.join("\n");
    text.truncate(text.trim_end().len());
    text.push('\n');
    let mut cmd = Command::new("git");
    cmd.env_clear()
        .env("PATH", std::env::var_os("PATH").unwrap_or_default())
        .env("HOME", "/nonexistent")
        .env("GIT_CONFIG_GLOBAL", "/dev/null")
        .env("GIT_CONFIG_SYSTEM", "/dev/null")
        .arg("interpret-trailers")
        .args(["--if-exists", "addIfDifferent"]);
    for t in trailers {
        cmd.args(["--trailer", t]);
    }
    let mut child = cmd
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|e| e.to_string())?;
    child
        .stdin
        .take()
        .ok_or("interpret-trailers has no stdin")?
        .write_all(text.as_bytes())
        .map_err(|e| e.to_string())?;
    let out = child.wait_with_output().map_err(|e| e.to_string())?;
    if !out.status.success() {
        return Err(format!(
            "git interpret-trailers failed: {}",
            String::from_utf8_lossy(&out.stderr).trim()
        ));
    }
    Ok(String::from_utf8_lossy(&out.stdout).into_owned())
}

/// The full id of the commit `rev` names; a revision that starts with `-` is never passed to git.
fn resolve(repo: &Path, rev: &str) -> Result<String, String> {
    if rev.is_empty() || rev.starts_with('-') {
        return Err(format!("cut: {rev:?} is not a commit"));
    }
    git::out(
        repo,
        &["rev-parse", "--verify", &format!("{rev}^{{commit}}")],
    )
    .map_err(|e| format!("cut: {rev} is not a commit: {e}"))
}

/// Cuts the branch; nothing is left behind when a pick or the gate check fails. The atomic creation of the
/// branch ref is the success boundary: scratch state that cannot be removed afterwards is reported on the
/// result lines, never as a failed cut.
pub fn cut(c: &Cut, python: &dyn Fn() -> Command) -> Result<Vec<String>, String> {
    let repo = c.repo;
    let ref_name = format!("refs/heads/{}", c.branch);
    git::out(repo, &["check-ref-format", &ref_name])?;
    if git::run(repo, None, &["rev-parse", "--verify", "--quiet", &ref_name])?
        .status
        .success()
    {
        return Err(format!("cut: branch {} already exists", c.branch));
    }
    let base = resolve(repo, c.base)?;
    // Every input is resolved once; the full id is what is validated, picked and recorded.
    let mut sources: Vec<String> = Vec::new();
    for rev in c.commits {
        let id = resolve(repo, rev)?;
        if sources.contains(&id) {
            return Err(format!("cut: {rev} is named twice"));
        }
        sources.push(id);
    }
    let patterns = pinned_gate_paths(c.gate, python)?;
    for src in &sources {
        let parents = git::out(repo, &["rev-list", "--parents", "-n", "1", src])?;
        if parents.split_whitespace().count() != 2 {
            return Err(format!("cut: {src} is not a single-parent commit"));
        }
        // Before the first pick: no gate path and no Gate-Edit line, visible or hidden.
        let message = git::out(repo, &["log", "-1", "--format=%B", src])?;
        refuse_gate_edit(repo, src, &message, &patterns)?;
    }
    // The pick and the check run in a private clone, so the repository's own configuration and
    // `info/attributes` cannot reach the merge machinery; the clone is a temporary directory owned by this
    // call and registered nowhere in the repository.
    let checkout = git::Checkout::new(repo, &base)?;
    let wt = checkout.path().to_owned();
    let picked = pick_all(&sources, &wt).and_then(|head| {
        check(&wt, &base, &head, c.gate, python)?;
        // The objects come over without a ref; the branch ref below is the only ref this call makes.
        checkout.adopt_objects(&head)?;
        Ok(head)
    });
    let created = picked.and_then(|head| {
        // An empty old value makes the update fail when the branch was created since the check above.
        git::out(repo, &["update-ref", &ref_name, &head, ""])
            .map_err(|e| format!("cut: branch {} already exists: {e}", c.branch))?;
        Ok(head)
    });
    let removed = checkout.dir.close().map_err(|e| e.to_string());
    let leftovers: Vec<String> = [removed
        .err()
        .map(|e| format!("the scratch clone {} not removed ({e})", wt.display()))]
    .into_iter()
    .flatten()
    .collect();
    let head = match created {
        Ok(head) => head,
        Err(e) if leftovers.is_empty() => return Err(e),
        Err(e) => return Err(format!("{e}\n{}", leftovers.join("; "))),
    };
    let mut lines = vec![format!(
        "cut {} {} commits, head {head}",
        c.branch,
        sources.len()
    )];
    lines.extend(leftovers.into_iter().map(|l| format!("warning: {l}")));
    Ok(lines)
}

fn pick_all(sources: &[String], wt: &Path) -> Result<String, String> {
    for src in sources {
        let o = git::run(wt, None, &["cherry-pick", "--allow-empty", src])?;
        if !o.status.success() {
            let _ = git::run(wt, None, &["cherry-pick", "--abort"]);
            return Err(format!(
                "cut: {src} does not apply: {}",
                String::from_utf8_lossy(&o.stderr).trim()
            ));
        }
        let trailers = vec![format!("Picked-From: {src}")];
        let message = with_trailers(&git::out(wt, &["log", "-1", "--format=%B"])?, &trailers)?;
        let mut amend = git::command(wt, None);
        amend.args(["commit", "--amend", "--allow-empty", "-q", "-F", "-"]);
        let mut child = amend
            .stdin(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .map_err(|e| e.to_string())?;
        child
            .stdin
            .take()
            .ok_or("commit has no stdin")?
            .write_all(message.as_bytes())
            .map_err(|e| e.to_string())?;
        let out = child.wait_with_output().map_err(|e| e.to_string())?;
        if !out.status.success() {
            return Err(format!(
                "cut: amending {src} failed: {}",
                String::from_utf8_lossy(&out.stderr).trim()
            ));
        }
    }
    git::out(wt, &["rev-parse", "HEAD"])
}

/// The environment of the gate's git commands is the cutter's: no repository override variable, no user or
/// system configuration, no replacement refs.
fn pin_git_env(cmd: &mut Command) {
    for (k, _) in std::env::vars_os() {
        if k.to_string_lossy().starts_with("GIT_") {
            cmd.env_remove(k);
        }
    }
    cmd.env("GIT_CONFIG_GLOBAL", "/dev/null")
        .env("GIT_CONFIG_SYSTEM", "/dev/null")
        .env("GIT_CONFIG_NOSYSTEM", "1")
        .env("GIT_NO_REPLACE_OBJECTS", "1")
        .env("GIT_TERMINAL_PROMPT", "0");
}

/// The check of `base..head`, the same for the cutter and for a branch cut by hand: no `cherry-pick -x` line
/// anywhere in a message, no gate path and no `Gate-Edit` line, then the commit gate (Writer trailer, gate
/// files) on the same range, given as full commit ids.
pub fn check(
    repo: &Path,
    base: &str,
    head: &str,
    gate: &Path,
    python: &dyn Fn() -> Command,
) -> Result<(), String> {
    let (base, head) = (resolve(repo, base)?, resolve(repo, head)?);
    let patterns = pinned_gate_paths(gate, python)?;
    for c in git::commits(repo, &format!("{base}..{head}"))? {
        let message = git::out(repo, &["log", "-1", "--format=%B", &c])?;
        if py_lines(&message).into_iter().any(is_picked_line) {
            return Err(format!(
                "{c}: a `(cherry picked from commit ...)` line ends the trailer block; cut with `land-train cut`, not `cherry-pick -x`"
            ));
        }
        refuse_gate_edit(repo, &c, &message, &patterns)?;
    }
    // The gate runs in `repo` (a temporary worktree for `cut`): a path relative to the caller's directory
    // must be made absolute before the directory changes.
    let gate =
        std::path::absolute(gate).map_err(|e| format!("gate path {}: {e}", gate.display()))?;
    let mut cmd = python();
    pin_git_env(&mut cmd);
    let out = cmd
        .arg(&gate)
        .args(["check", "--base", &base, "--head", &head])
        .current_dir(repo)
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

/// `cut --repo DIR --base REV --branch NAME --commits A,B` and `cut-check --repo DIR --base REV --head REV`.
/// The commit gate is `tools/ci_commit_gate.py` of the base commit, as CI reads it; there is no flag to name
/// another (tests give `Cut::gate` directly). `--grants` and `--gate-edits` are refused: no grant is accepted.
pub fn run_cli(
    cmd: &str,
    flags: &BTreeMap<String, String>,
    python: &dyn Fn() -> Command,
) -> Result<Vec<String>, String> {
    let need = |k: &str| {
        flags
            .get(k)
            .cloned()
            .ok_or_else(|| format!("{cmd}: missing --{k}"))
    };
    for flag in ["grants", "gate-edits"] {
        if flags.contains_key(flag) {
            return Err(grants_refused(cmd, flag));
        }
    }
    if flags.contains_key("gate") {
        return Err(format!(
            "{cmd}: --gate is not accepted; the gate is read from the base commit"
        ));
    }
    let repo = PathBuf::from(need("repo")?);
    let base = resolve(&repo, &need("base")?)?;
    let scratch = tempfile::tempdir().map_err(|e| e.to_string())?;
    let gate = scratch.path().join("ci_commit_gate.py");
    std::fs::write(
        &gate,
        git::bytes(&repo, &["show", &format!("{base}:tools/ci_commit_gate.py")])?,
    )
    .map_err(|e| e.to_string())?;
    if cmd == "cut-check" {
        check(&repo, &base, &need("head")?, &gate, python)?;
        return Ok(vec!["commit gate accepts the range".into()]);
    }
    let commits: Vec<String> = need("commits")?
        .split(',')
        .filter(|s| !s.is_empty())
        .map(str::to_owned)
        .collect();
    cut(
        &Cut {
            repo: &repo,
            base: &base,
            branch: &need("branch")?,
            commits: &commits,
            gate: &gate,
        },
        python,
    )
}
