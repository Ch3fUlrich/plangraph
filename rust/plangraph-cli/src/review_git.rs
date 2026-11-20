//! Git freshness, writer-family trailer extraction, and per-file diff helpers for `plangraph review`.
use pg_worker_core::review_split::valid_diff_path;
use regex::Regex;
use std::collections::BTreeSet;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::OnceLock;

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

fn origin_fresh_ref(repo: &Path, base: &str) -> Option<(String, String)> {
    if let Ok(head_sym) = git_text(
        repo,
        &["symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
    ) {
        if let Ok(sha) = resolve_commit(repo, &head_sym) {
            return Some((head_sym, sha));
        }
    }
    if let Ok(sha) = resolve_commit(repo, "refs/remotes/origin/main") {
        return Some(("origin/main".to_owned(), sha));
    }
    if base.starts_with("origin/") || base.starts_with("refs/remotes/origin/") {
        if let Ok(sha) = resolve_commit(repo, base) {
            return Some((base.to_owned(), sha));
        }
    }
    None
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

    if fetch_origin && git_text(repo, &["remote", "get-url", "origin"]).is_ok() {
        git_text(repo, &["fetch", "--quiet", "origin"])?;
    }

    let head_sha = resolve_commit(repo, head)?;
    let base_sha = resolve_commit(repo, base)?;

    if let Some((fresh_name, fresh_sha)) = origin_fresh_ref(repo, base) {
        let range = format!("{head_sha}..{fresh_sha}");
        let count_str = git_text(repo, &["rev-list", "--count", &range])?;
        let behind_origin = count_str
            .parse::<usize>()
            .map_err(|e| format!("invalid rev-list count {count_str:?}: {e}"))?;
        if behind_origin > 0 {
            return Err(format!(
                "refused: base_stale: head {} is behind {} by {} commit(s) (D-5661)",
                &head_sha[..12.min(head_sha.len())],
                fresh_name,
                behind_origin
            ));
        }
    }

    let diff_range = format!("{base_sha}...{head_sha}");
    let changed = git_stdout(repo, &["diff", "--name-only", "-z", &diff_range])?;
    if changed.iter().all(|&b| b == 0) {
        return Err(format!(
            "refused: empty_diff: no changed files in {}...{}",
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

fn family_patterns() -> &'static Vec<(Regex, &'static str)> {
    static PATTERNS: OnceLock<Vec<(Regex, &'static str)>> = OnceLock::new();
    PATTERNS.get_or_init(|| {
        [
            (r"\bopenai\b|\bgpt\b|\bcodex\b|\bo[134]\b", "openai"),
            (
                r"\banthropic\b|\bclaude\b|\bsonnet\b|\bopus\b|\bhaiku\b",
                "anthropic",
            ),
            (r"\bgoogle\b|\bgemini\b", "google"),
            (r"\bxiaomi\b|\bmimo\b", "xiaomi"),
            (r"\bdeepseek\b", "deepseek"),
            (r"\balibaba\b|\bqwen\b", "alibaba"),
            (r"\bmeta\b|\bllama\b|\bmuse\b", "meta"),
            (r"\bzhipu\b|\bglm\b", "zhipu"),
            (r"\bnvidia\b|\bnemotron\b", "nvidia"),
            (r"\bxai\b|\bgrok\b", "xai"),
        ]
        .into_iter()
        .map(|(pat, fam)| (Regex::new(pat).expect("valid family regex"), fam))
        .collect()
    })
}

/// Normalize a raw writer/author string into canonical spawner family names.
pub fn extract_families_from_text(raw: &str) -> Vec<String> {
    let lower = raw.to_ascii_lowercase();
    let normalized = lower.replace(['-', '_', '/', '.', '@', ':', ','], " ");
    let mut found = BTreeSet::new();
    for (re, fam) in family_patterns() {
        if re.is_match(&normalized) {
            found.insert((*fam).to_owned());
        }
    }
    found.into_iter().collect()
}

fn is_non_family_hand_edit(value: &str) -> bool {
    let lower = value.trim().to_ascii_lowercase();
    lower.starts_with("changelog")
        || lower.starts_with("docs")
        || lower.starts_with("status")
        || lower.starts_with("rebase")
        || lower.starts_with("merge")
}

/// Extract canonical writer families from `Writer`, `Hand-Edit`, and fallback author info in `base..head`.
pub fn writer_families(repo: &Path, base_sha: &str, head_sha: &str) -> Result<Vec<String>, String> {
    let range = format!("{base_sha}..{head_sha}");
    let mut raw = git_stdout(
        repo,
        &[
            "log",
            "--no-merges",
            "-z",
            "--format=%H%n%an%n%ae%n%(trailers:separator=%x0a)",
            &range,
        ],
    )?;
    if raw.is_empty() {
        raw = git_stdout(
            repo,
            &[
                "log",
                "-z",
                "--format=%H%n%an%n%ae%n%(trailers:separator=%x0a)",
                &range,
            ],
        )?;
    }
    if raw.is_empty() {
        return Err(format!(
            "refused: no commits found in {}..{}",
            &base_sha[..12.min(base_sha.len())],
            &head_sha[..12.min(head_sha.len())]
        ));
    }

    let mut families = BTreeSet::new();
    for chunk in raw.split(|&b| b == 0) {
        if chunk.is_empty() {
            continue;
        }
        let text = String::from_utf8_lossy(chunk);
        let mut lines = text.splitn(4, '\n');
        let sha = lines.next().unwrap_or("").trim();
        let author_name = lines.next().unwrap_or("").trim();
        let author_email = lines.next().unwrap_or("").trim();
        let trailers = lines.next().unwrap_or("");

        let mut explicit_on_commit = false;
        let mut fallback_trailers = Vec::new();

        for line in trailers.lines() {
            let trimmed = line.trim();
            if trimmed.is_empty() {
                continue;
            }
            let Some((key, val)) = trimmed.split_once(':') else {
                continue;
            };
            let key_lower = key.trim().to_ascii_lowercase();
            let val_trim = val.trim();
            match key_lower.as_str() {
                "writer" => {
                    explicit_on_commit = true;
                    let matched = extract_families_from_text(val_trim);
                    if matched.is_empty() {
                        return Err(format!(
                            "refused: unknown writer family in Writer trailer on {}: {}",
                            &sha[..12.min(sha.len())],
                            val_trim
                        ));
                    }
                    families.extend(matched);
                }
                "hand-edit" => {
                    let matched = extract_families_from_text(val_trim);
                    if !matched.is_empty() {
                        explicit_on_commit = true;
                        families.extend(matched);
                    } else if !is_non_family_hand_edit(val_trim) && val_trim != "none" {
                        // A human/unclassified Hand-Edit without Writer still records intent if explicit.
                        fallback_trailers.push(val_trim.to_owned());
                    }
                }
                "author" | "co-authored-by" | "signed-off-by" => {
                    fallback_trailers.push(val_trim.to_owned());
                }
                _ => {}
            }
        }

        if !explicit_on_commit {
            let mut found_from_fallback = false;
            for item in &fallback_trailers {
                let matched = extract_families_from_text(item);
                if !matched.is_empty() {
                    found_from_fallback = true;
                    families.extend(matched);
                }
            }
            if !found_from_fallback {
                let author_combined = format!("{author_name} {author_email}");
                families.extend(extract_families_from_text(&author_combined));
            }
        }
    }

    if families.is_empty() {
        return Err(format!(
            "refused: could not determine any writer family from trailers/authors in {}..{}",
            &base_sha[..12.min(base_sha.len())],
            &head_sha[..12.min(head_sha.len())]
        ));
    }
    Ok(families.into_iter().collect())
}

/// Read per-file diff byte payloads for `base_sha...head_sha`.
pub fn measure_diff_files(
    repo: &Path,
    base_sha: &str,
    head_sha: &str,
) -> Result<Vec<(String, Vec<u8>)>, String> {
    let range = format!("{base_sha}...{head_sha}");
    let raw_names = git_stdout(repo, &["diff", "--name-only", "-z", &range])?;
    let mut out = Vec::new();
    for part in raw_names.split(|&b| b == 0) {
        if part.is_empty() {
            continue;
        }
        let path = String::from_utf8(part.to_vec())
            .map_err(|_| "refused: non-utf8 path in diff".to_owned())?;
        if !valid_diff_path(&path) {
            return Err(format!("refused: invalid diff path: {path}"));
        }
        let diff_bytes = git_stdout(repo, &["diff", &range, "--", &path])?;
        out.push((path, diff_bytes));
    }
    Ok(out)
}
