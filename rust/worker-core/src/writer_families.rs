//! Writer families of a commit range, read from its `Writer`/`Hand-Edit` trailers: the one
//! exclusion set every review of that range must avoid (rule 3.58 (1)). Shared by
//! `plangraph review` and the native writer's in-session review.
use crate::git::git_bytes;
use serde_json::Value;
use std::collections::{BTreeSet, HashMap};
use std::io::Write;
use std::path::Path;
use std::process::{Command, Stdio};

/// Python interpreter command shared by every adapter to the Python helpers.
/// `src_root` (a checkout holding `src/plangraph`) is put on `PYTHONPATH` when it exists.
pub fn python_command(src_root: Option<&Path>) -> Command {
    let mut cmd = Command::new(
        std::env::var_os("PG_SPAWND_PYTHON")
            .unwrap_or_else(|| if cfg!(windows) { "python" } else { "python3" }.into()),
    );
    if let Some(src) = src_root
        .map(|root| root.join("src"))
        .filter(|s| s.join("plangraph").is_dir())
    {
        let sep = if cfg!(windows) { ";" } else { ":" };
        let existing = std::env::var("PYTHONPATH").unwrap_or_default();
        let joined = if existing.is_empty() {
            src.display().to_string()
        } else {
            format!("{}{sep}{existing}", src.display())
        };
        cmd.env("PYTHONPATH", joined);
    }
    cmd
}

fn log_bytes(repo: &Path, args: &[&str]) -> Result<Vec<u8>, String> {
    git_bytes(repo, args).map_err(|e| e.to_string())
}

/// Run `code` with `python`, feeding `payload` on stdin; returns stdout. `what` names the
/// helper in refusals.
fn run_python(
    python: &dyn Fn() -> Command,
    code: &str,
    payload: Vec<u8>,
    what: &str,
) -> Result<Vec<u8>, String> {
    let mut child = python()
        .args(["-c", code])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|e| format!("refused: {what} unavailable: {e}"))?;
    let mut stdin = child
        .stdin
        .take()
        .ok_or_else(|| format!("refused: {what} has no stdin"))?;
    let writer = std::thread::spawn(move || stdin.write_all(&payload));
    let out = child
        .wait_with_output()
        .map_err(|e| format!("refused: {what} failed: {e}"))?;
    let _ = writer.join();
    if !out.status.success() {
        return Err(format!(
            "refused: {what} failed: {}",
            String::from_utf8_lossy(&out.stderr).trim()
        ));
    }
    Ok(out.stdout)
}

/// The versioned review brief (`plangraph.review.prompt.render_brief("review", context,
/// rules=rules)`): its fixed prefix, then `rules`, then the item-context block for the fields
/// `context` names.
pub fn render_review_brief(
    context: &Value,
    rules: &str,
    python: &dyn Fn() -> Command,
) -> Result<String, String> {
    const CODE: &str = "import json, sys; from plangraph.review.prompt import render_brief; a = json.load(sys.stdin); sys.stdout.write(render_brief('review', a['context'], rules=a['rules']))";
    let payload = serde_json::to_vec(&serde_json::json!({"context": context, "rules": rules}))
        .map_err(|e| format!("serialize brief context: {e}"))?;
    let out = run_python(python, CODE, payload, "review brief")?;
    String::from_utf8(out).map_err(|e| format!("refused: review brief output: {e}"))
}

/// Map each text to its model family through `plangraph.review.family.model_family`,
/// the one name-to-family table; `None` means unknown or ambiguous.
fn resolve_families(
    texts: &BTreeSet<String>,
    python: &dyn Fn() -> Command,
) -> Result<HashMap<String, Option<String>>, String> {
    const CODE: &str = "import json, sys; from plangraph.review.family import model_family; print(json.dumps({t: model_family(t) for t in json.load(sys.stdin)}))";
    let payload = serde_json::to_vec(&texts.iter().collect::<Vec<_>>())
        .map_err(|e| format!("serialize family texts: {e}"))?;
    let out = run_python(python, CODE, payload, "family table")?;
    let parsed: Value =
        serde_json::from_slice(&out).map_err(|e| format!("refused: family table output: {e}"))?;
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

/// One commit's attribution strings, before they are mapped to families.
struct CommitText {
    sha: String,
    writers: Vec<String>,
    hand_edits: Vec<String>,
    fallback: Vec<String>,
    author: String,
    merge: bool,
}

fn short(sha: &str) -> &str {
    &sha[..12.min(sha.len())]
}

/// Every writer family of `base..head`: the canonical families of the `Writer` and `Hand-Edit`
/// trailers of each commit, else of its other attribution lines and author. `leg_models` are
/// models that wrote without a trailer (a native writer's served model); their families join
/// the result and an unknown one is refused. `python` starts the interpreter that holds
/// `plangraph.review.family`. This is the one reader of the review exclusion set.
pub fn writer_families(
    repo: &Path,
    base_sha: &str,
    head_sha: &str,
    leg_models: &[&str],
    python: &dyn Fn() -> Command,
) -> Result<Vec<String>, String> {
    let read = commit_families(repo, base_sha, head_sha, leg_models, python)?;
    let all: BTreeSet<String> = read
        .models
        .into_iter()
        .chain(
            read.per_commit
                .into_iter()
                .flat_map(|(_, families)| families),
        )
        .collect();
    if all.is_empty() {
        return Err(format!(
            "refused: could not determine any writer family from trailers/authors in {}..{}",
            short(base_sha),
            short(head_sha)
        ));
    }
    Ok(all.into_iter().collect())
}

/// The writer families of every commit of a range, one set per commit (log order), and the
/// families of the `leg_models`.
pub struct RangeFamilies {
    pub per_commit: Vec<(String, BTreeSet<String>)>,
    pub models: BTreeSet<String>,
}

/// Per-commit form of [`writer_families`]: the same reading rules, kept per commit so a review can
/// give each writer-family group its own seat. A merge with ordinary commits around it, or a commit
/// with nothing resolvable, has an empty set.
pub fn commit_families(
    repo: &Path,
    base_sha: &str,
    head_sha: &str,
    leg_models: &[&str],
    python: &dyn Fn() -> Command,
) -> Result<RangeFamilies, String> {
    let range = format!("{base_sha}..{head_sha}");
    // Merge commits are read too: their Writer trailers name who resolved the merge.
    let format = "--format=%H%n%P%n%an%n%ae%n%(trailers:separator=%x0a)";
    let raw = log_bytes(repo, &["log", "-z", format, &range])?;
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
        let mut lines = text.splitn(5, '\n');
        let sha = lines.next().unwrap_or("").trim().to_owned();
        let merge = lines.next().unwrap_or("").split_whitespace().count() > 1;
        let name = lines.next().unwrap_or("").trim();
        let email = lines.next().unwrap_or("").trim();
        let mut commit = CommitText {
            sha,
            writers: Vec::new(),
            hand_edits: Vec::new(),
            fallback: Vec::new(),
            author: format!("{name} {email}"),
            merge,
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
    let texts: BTreeSet<String> = texts
        .into_iter()
        .chain(leg_models.iter().map(|m| (*m).to_owned()))
        .collect();
    let table = resolve_families(&texts, python)?;
    let family_of = |text: &str| table.get(text).cloned().flatten();

    let has_ordinary = commits.iter().any(|c| !c.merge);
    let mut models = BTreeSet::new();
    for model in leg_models {
        match family_of(model) {
            Some(f) => {
                models.insert(f);
            }
            None => return Err(format!("refused: unknown writer family of model {model}")),
        }
    }
    let mut per_commit = Vec::new();
    for c in &commits {
        let mut families = BTreeSet::new();
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
        // A merge's author is only evidence when the range holds nothing but merges.
        if !(explicit || (c.merge && has_ordinary)) {
            let found: Vec<String> = fallback.iter().filter_map(|t| family_of(t)).collect();
            if found.is_empty() {
                families.extend(family_of(&c.author));
            } else {
                families.extend(found);
            }
        }
        per_commit.push((c.sha.clone(), families));
    }
    Ok(RangeFamilies { per_commit, models })
}

#[cfg(test)]
#[path = "writer_families_tests.rs"]
mod tests;
