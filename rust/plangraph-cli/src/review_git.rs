//! Git freshness, writer-family trailer extraction, and per-file diff helpers for `plangraph review`.
use crate::base_fresh::{check_ref, python_command};
use pg_worker_core::review_split::valid_diff_path;
use serde_json::Value;
use std::collections::{BTreeSet, HashMap};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};

#[derive(Debug, Clone)]
pub struct ResolvedRange {
    pub repo_root: PathBuf,
    pub git_common_dir: PathBuf,
    pub base_sha: String,
    pub head_sha: String,
}

fn git_stdout(repo: &Path, args: &[&str]) -> Result<Vec<u8>, String> {
    let out = Command::new("git")
        .arg("-C")
        .arg(repo)
        .args(args)
        .output()
        .map_err(|e| format!("failed to run git {}: {e}", args.join(" ")))?;
    if !out.status.success() {
        let err = String::from_utf8_lossy(&out.stderr).trim().to_owned();
        return Err(if err.is_empty() {
            format!(
                "git {} exited with {}",
                args.join(" "),
                out.status.code().unwrap_or(1)
            )
        } else {
            format!("git {}: {err}", args.join(" "))
        });
    }
    Ok(out.stdout)
}

fn git_text(repo: &Path, args: &[&str]) -> Result<String, String> {
    let bytes = git_stdout(repo, args)?;
    Ok(String::from_utf8_lossy(&bytes).trim().to_owned())
}

fn resolve_commit(repo: &Path, rev: &str) -> Result<String, String> {
    let spec = format!("{rev}^{{commit}}");
    git_text(repo, &["rev-parse", "--verify", &spec])
}

/// Resolve repo paths, verify D-5661 freshness against `origin`, and ensure a non-empty diff.
pub fn resolve_and_check_fresh(
    repo: &Path,
    base: &str,
    head: &str,
    fetch_origin: bool,
) -> Result<ResolvedRange, String> {
    let top = git_text(repo, &["rev-parse", "--show-toplevel"])?;
    let repo_root = PathBuf::from(&top)
        .canonicalize()
        .unwrap_or_else(|_| PathBuf::from(&top));
    let common_raw = git_text(repo, &["rev-parse", "--git-common-dir"])?;
    let common_path = Path::new(&common_raw);
    let git_common_dir = if common_path.is_absolute() {
        common_path.to_path_buf()
    } else {
        repo_root.join(common_path)
    }
    .canonicalize()
    .unwrap_or_else(|_| repo_root.join(".git"));

    let head_sha = resolve_commit(repo, head)?;
    let base_sha = resolve_commit(repo, base)?;

    // The one D-5661 gate (plangraph.spawner.base_fresh) decides; it refuses when no origin ref resolves.
    check_ref(
        &repo_root.to_string_lossy(),
        &head_sha,
        fetch_origin,
        Some(&repo_root),
    )
    .map_err(|reason| format!("refused: {reason}"))?;

    let changed = changed_names(repo, &base_sha, &head_sha)?;
    if changed.iter().all(|&b| b == 0) {
        return Err(format!(
            "refused: empty_diff: no changed files in {}..{}",
            &base_sha[..12.min(base_sha.len())],
            &head_sha[..12.min(head_sha.len())]
        ));
    }

    Ok(ResolvedRange {
        repo_root,
        git_common_dir,
        base_sha,
        head_sha,
    })
}

/// Map each text to its model family through `plangraph.review.family.model_family`,
/// the one name-to-family table; `None` means unknown or ambiguous.
fn resolve_families(
    texts: &BTreeSet<String>,
    src_root: &Path,
) -> Result<HashMap<String, Option<String>>, String> {
    const CODE: &str = "import json, sys; from plangraph.review.family import model_family; print(json.dumps({t: model_family(t) for t in json.load(sys.stdin)}))";
    let mut child = python_command(Some(src_root))
        .args(["-c", CODE])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|e| format!("refused: family table unavailable: {e}"))?;
    let payload = serde_json::to_vec(&texts.iter().collect::<Vec<_>>())
        .map_err(|e| format!("serialize family texts: {e}"))?;
    let mut stdin = child
        .stdin
        .take()
        .ok_or("refused: family table has no stdin")?;
    let writer = std::thread::spawn(move || stdin.write_all(&payload));
    let out = child
        .wait_with_output()
        .map_err(|e| format!("refused: family table failed: {e}"))?;
    let _ = writer.join();
    if !out.status.success() {
        return Err(format!(
            "refused: family table failed: {}",
            String::from_utf8_lossy(&out.stderr).trim()
        ));
    }
    let parsed: Value = serde_json::from_slice(&out.stdout)
        .map_err(|e| format!("refused: family table output: {e}"))?;
    Ok(parsed
        .as_object()
        .map(|m| {
            m.iter()
                .map(|(k, v)| (k.clone(), v.as_str().map(str::to_owned)))
                .collect()
        })
        .unwrap_or_default())
}

fn is_non_family_hand_edit(value: &str) -> bool {
    let lower = value.trim().to_ascii_lowercase();
    lower.starts_with("changelog")
        || lower.starts_with("docs")
        || lower.starts_with("status")
        || lower.starts_with("rebase")
        || lower.starts_with("merge")
}

/// One non-merge commit's attribution strings, before they are mapped to families.
struct CommitText {
    sha: String,
    writers: Vec<String>,
    hand_edits: Vec<String>,
    fallback: Vec<String>,
    author: String,
}

fn short(sha: &str) -> &str {
    &sha[..12.min(sha.len())]
}

/// Extract canonical writer families from `Writer`, `Hand-Edit`, and fallback author info in `base..head`.
pub fn writer_families(repo: &Path, base_sha: &str, head_sha: &str) -> Result<Vec<String>, String> {
    let range = format!("{base_sha}..{head_sha}");
    let format = "--format=%H%n%an%n%ae%n%(trailers:separator=%x0a)";
    let mut raw = git_stdout(repo, &["log", "--no-merges", "-z", format, &range])?;
    if raw.is_empty() {
        raw = git_stdout(repo, &["log", "-z", format, &range])?;
    }
    if raw.is_empty() {
        return Err(format!(
            "refused: no commits found in {}..{}",
            short(base_sha),
            short(head_sha)
        ));
    }

    let mut commits = Vec::new();
    for chunk in raw.split(|&b| b == 0).filter(|c| !c.is_empty()) {
        let text = String::from_utf8_lossy(chunk);
        let mut lines = text.splitn(4, '\n');
        let sha = lines.next().unwrap_or("").trim().to_owned();
        let name = lines.next().unwrap_or("").trim();
        let email = lines.next().unwrap_or("").trim();
        let mut commit = CommitText {
            sha,
            writers: Vec::new(),
            hand_edits: Vec::new(),
            fallback: Vec::new(),
            author: format!("{name} {email}"),
        };
        for line in lines.next().unwrap_or("").lines() {
            let Some((key, val)) = line.trim().split_once(':') else {
                continue;
            };
            let val = val.trim().to_owned();
            match key.trim().to_ascii_lowercase().as_str() {
                "writer" => commit.writers.push(val),
                "hand-edit" => commit.hand_edits.push(val),
                "author" | "co-authored-by" | "signed-off-by" => commit.fallback.push(val),
                _ => {}
            }
        }
        commits.push(commit);
    }

    let texts: BTreeSet<String> = commits
        .iter()
        .flat_map(|c| {
            c.writers
                .iter()
                .chain(&c.hand_edits)
                .chain(&c.fallback)
                .chain(std::iter::once(&c.author))
        })
        .cloned()
        .collect();
    let table = resolve_families(&texts, repo)?;
    let family_of = |text: &str| table.get(text).cloned().flatten();

    let mut families = BTreeSet::new();
    for c in &commits {
        let mut explicit = !c.writers.is_empty();
        for w in &c.writers {
            match family_of(w) {
                Some(f) => {
                    families.insert(f);
                }
                None => {
                    return Err(format!(
                        "refused: unknown writer family in Writer trailer on {}: {w}",
                        short(&c.sha)
                    ));
                }
            }
        }
        let mut fallback = c.fallback.clone();
        for h in &c.hand_edits {
            if let Some(f) = family_of(h) {
                explicit = true;
                families.insert(f);
            } else if !is_non_family_hand_edit(h) && h != "none" {
                fallback.push(h.clone());
            }
        }
        if explicit {
            continue;
        }
        let found: Vec<String> = fallback.iter().filter_map(|t| family_of(t)).collect();
        if found.is_empty() {
            families.extend(family_of(&c.author));
        } else {
            families.extend(found);
        }
    }

    if families.is_empty() {
        return Err(format!(
            "refused: could not determine any writer family from trailers/authors in {}..{}",
            short(base_sha),
            short(head_sha)
        ));
    }
    Ok(families.into_iter().collect())
}

// Same flags as pg-spawnd's inline diff (prompt_git.rs), so the CLI measures what the seat gets.
const DIFF_FLAGS: [&str; 5] = [
    "diff",
    "--no-ext-diff",
    "--no-textconv",
    "--no-renames",
    "--binary",
];

fn changed_names(repo: &Path, base_sha: &str, head_sha: &str) -> Result<Vec<u8>, String> {
    git_stdout(
        repo,
        &[
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--no-renames",
            "--name-only",
            "-z",
            base_sha,
            head_sha,
            "--",
        ],
    )
}

/// Read per-file diff byte payloads for `base_sha` to `head_sha`, as the spawner inlines them.
pub fn measure_diff_files(
    repo: &Path,
    base_sha: &str,
    head_sha: &str,
) -> Result<Vec<(String, Vec<u8>)>, String> {
    let raw_names = changed_names(repo, base_sha, head_sha)?;
    let mut out = Vec::new();
    for part in raw_names.split(|&b| b == 0).filter(|p| !p.is_empty()) {
        let path = String::from_utf8(part.to_vec())
            .map_err(|_| "refused: non-utf8 path in diff".to_owned())?;
        if !valid_diff_path(&path) {
            return Err(format!("refused: invalid diff path: {path}"));
        }
        let mut args: Vec<&str> = DIFF_FLAGS.to_vec();
        args.extend([base_sha, head_sha, "--", &path]);
        let bytes = git_stdout(repo, &args)?;
        out.push((path, bytes));
    }
    Ok(out)
}
