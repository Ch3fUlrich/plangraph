//! `land-train cut`: cuts a slice or landing branch from source commits without breaking the trailer block.
//! Commits are picked without `-x`; the source id goes into a `Picked-From:` trailer inside the block, a
//! router-granted `Gate-Edit:` is added inside the same block, and `ci_commit_gate.py check` must accept the
//! result before the branch exists. `check` is the same test for a branch cut by hand.
use super::git;
use std::collections::BTreeMap;
use std::io::Write;
use std::path::Path;
use std::process::{Command, Stdio};

pub struct Cut<'a> {
    pub repo: &'a Path,
    pub base: &'a str,
    /// The branch to create; it must not exist.
    pub branch: &'a str,
    /// Source commits, oldest first.
    pub commits: &'a [String],
    /// Source commit -> the router's D-id for its gate-file edit.
    pub gate_edits: &'a BTreeMap<String, String>,
    /// The D-ids the router's GO row grants; a gate edit may carry only one of them.
    pub grants: &'a [String],
    /// `tools/ci_commit_gate.py` of the checkout that runs the cut.
    pub gate: &'a Path,
}

/// A `(cherry picked from commit ...)` line is what `cherry-pick -x` leaves; inside the trailer paragraph
/// it ends the block, so the cutter writes `Picked-From:` instead and drops the line.
fn is_picked_line(line: &str) -> bool {
    line.starts_with("(cherry picked from commit ")
}

/// The message with the `-x` line removed and the trailers added inside its trailer block.
fn with_trailers(message: &str, trailers: &[String]) -> Result<String, String> {
    let kept: Vec<&str> = message.lines().filter(|l| !is_picked_line(l)).collect();
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

/// Cuts the branch; nothing is left behind when a pick or the gate check fails.
pub fn cut(c: &Cut, python: &dyn Fn() -> Command) -> Result<Vec<String>, String> {
    let repo = c.repo;
    git::out(
        repo,
        &["check-ref-format", &format!("refs/heads/{}", c.branch)],
    )?;
    if git::run(
        repo,
        None,
        &[
            "rev-parse",
            "--verify",
            "--quiet",
            &format!("refs/heads/{}", c.branch),
        ],
    )?
    .status
    .success()
    {
        return Err(format!("cut: branch {} already exists", c.branch));
    }
    let base = git::out(
        repo,
        &["rev-parse", "--verify", &format!("{}^{{commit}}", c.base)],
    )?;
    for (src, d) in c.gate_edits {
        if !c.commits.contains(src) {
            return Err(format!("cut: gate edit names {src}, which is not cut"));
        }
        if !c.grants.contains(d) {
            return Err(format!("cut: {d} for {src} is not granted by the GO row"));
        }
    }
    for src in c.commits {
        let parents = git::out(repo, &["rev-list", "--parents", "-n", "1", src])?;
        if parents.split_whitespace().count() != 2 {
            return Err(format!("cut: {src} is not a single-parent commit"));
        }
    }
    let common = git::out(
        repo,
        &["rev-parse", "--path-format=absolute", "--git-common-dir"],
    )?;
    let parent = Path::new(&common).join("plangraph/cut-worktrees");
    std::fs::create_dir_all(&parent).map_err(|e| e.to_string())?;
    let tmp = tempfile::Builder::new()
        .prefix("cut-")
        .tempdir_in(&parent)
        .map_err(|e| e.to_string())?;
    let wt = tmp.path();
    git::out(
        repo,
        &["worktree", "add", "--detach", &wt.to_string_lossy(), &base],
    )?;
    let result = pick_all(c, wt).and_then(|head| {
        check(wt, &base, &head, c.gate, python)?;
        // An empty old value makes the update fail when the branch was created since the check above.
        git::out(
            repo,
            &["update-ref", &format!("refs/heads/{}", c.branch), &head, ""],
        )
        .map_err(|e| format!("cut: branch {} already exists: {e}", c.branch))?;
        Ok(head)
    });
    let removed = git::out(
        repo,
        &["worktree", "remove", "--force", &wt.to_string_lossy()],
    );
    let head = result?;
    removed?;
    Ok(vec![format!(
        "cut {} {} commits, head {head}",
        c.branch,
        c.commits.len()
    )])
}

fn pick_all(c: &Cut, wt: &Path) -> Result<String, String> {
    for src in c.commits {
        let o = git::run(wt, None, &["cherry-pick", "--allow-empty", src])?;
        if !o.status.success() {
            let _ = git::run(wt, None, &["cherry-pick", "--abort"]);
            return Err(format!(
                "cut: {src} does not apply: {}",
                String::from_utf8_lossy(&o.stderr).trim()
            ));
        }
        let mut trailers = vec![format!("Picked-From: {src}")];
        if let Some(d) = c.gate_edits.get(src) {
            trailers.push(format!("Gate-Edit: {d}"));
        }
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

/// The cutter's own check of `base..head`: no `cherry-pick -x` line anywhere in a message, then the commit
/// gate (Writer trailer, Gate-Edit for gate files) on the same range.
pub fn check(
    repo: &Path,
    base: &str,
    head: &str,
    gate: &Path,
    python: &dyn Fn() -> Command,
) -> Result<(), String> {
    for c in git::commits(repo, &format!("{base}..{head}"))? {
        let message = git::out(repo, &["log", "-1", "--format=%B", &c])?;
        if message.lines().any(is_picked_line) {
            return Err(format!(
                "{c}: a `(cherry picked from commit ...)` line ends the trailer block; cut with `land-train cut`, not `cherry-pick -x`"
            ));
        }
    }
    // The gate runs in `repo` (a temporary worktree for `cut`): a path relative to the caller's directory
    // must be made absolute before the directory changes.
    let gate =
        std::path::absolute(gate).map_err(|e| format!("gate path {}: {e}", gate.display()))?;
    let out = python()
        .arg(&gate)
        .args(["check", "--base", base, "--head", head])
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

/// `cut --repo DIR --base SHA --branch NAME --commits A,B [--gate-edits A=D-1,B=D-2 --grants FILE] [--gate FILE]`
/// (FILE lists the D-ids of the GO row) and `cut-check --repo DIR --base SHA --head SHA [--gate FILE]`.
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
    let repo = std::path::PathBuf::from(need("repo")?);
    let gate = flags
        .get("gate")
        .map(std::path::PathBuf::from)
        .unwrap_or_else(|| repo.join("tools/ci_commit_gate.py"));
    if cmd == "cut-check" {
        check(&repo, &need("base")?, &need("head")?, &gate, python)?;
        return Ok(vec!["commit gate accepts the range".into()]);
    }
    let commits: Vec<String> = need("commits")?
        .split(',')
        .filter(|s| !s.is_empty())
        .map(str::to_owned)
        .collect();
    let grants: Vec<String> = match flags.get("grants") {
        Some(f) => std::fs::read_to_string(f)
            .map_err(|e| format!("cut: grants {f}: {e}"))?
            .split_whitespace()
            .map(str::to_owned)
            .collect(),
        None => Vec::new(),
    };
    let gate_edits = flags
        .get("gate-edits")
        .map(|s| {
            s.split(',')
                .filter_map(|p| p.split_once('='))
                .map(|(a, b)| (a.to_owned(), b.to_owned()))
                .collect()
        })
        .unwrap_or_default();
    cut(
        &Cut {
            repo: &repo,
            base: &need("base")?,
            branch: &need("branch")?,
            commits: &commits,
            gate_edits: &gate_edits,
            grants: &grants,
            gate: &gate,
        },
        python,
    )
}
