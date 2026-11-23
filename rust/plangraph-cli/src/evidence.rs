//! Validate evidence articles and generate the oversight index from their front matter.

mod front_matter;

use crate::atomic;
use regex::Regex;
use serde_json::Value;
use std::collections::HashSet;
use std::fs;
use std::path::{Component, Path, PathBuf};

const USAGE: &str = "usage: plangraph evidence <check|index> [EVIDENCE_DIR]";

pub fn run(args: &[String]) -> i32 {
    let (action, root) = match args {
        [help] if matches!(help.as_str(), "-h" | "--help") => {
            println!("{USAGE}");
            return 0;
        }
        [action] => (action.as_str(), Path::new("evidence")),
        [action, root] => (action.as_str(), Path::new(root)),
        _ => ("", Path::new("evidence")),
    };
    let write_index = match action {
        "check" => false,
        "index" => true,
        _ => {
            eprintln!("{USAGE}");
            return 2;
        }
    };
    match process(root, write_index) {
        Ok((articles, results)) => {
            let outcome = if write_index {
                "wrote index.jsonl for"
            } else {
                "checked"
            };
            println!("evidence: {outcome} {articles} article(s), {results} result(s)");
            0
        }
        Err(reason) => {
            eprintln!("evidence: {reason}");
            1
        }
    }
}

fn process(root: &Path, write_index: bool) -> Result<(usize, usize), String> {
    let dirs = article_dirs(root)?;
    let brackets = Regex::new(r"\[([^\[\]\r\n]+)\]").expect("valid bracket pattern");
    let mut index = Vec::new();
    let mut results = 0;
    for (dir, name) in &dirs {
        let article = dir.join("article.md");
        let path = format!("evidence/{name}/article.md");
        let text = read_article_file(dir, "article.md")?;
        let parsed = front_matter::parse(&text).map_err(|reason| at(&article, reason))?;
        let claims = load_claims(dir)?;
        for id in parsed
            .claim_ids()
            .chain(prose_claim_ids(parsed.body, &brackets))
        {
            if !claims.contains(id) {
                return Err(at(
                    &article,
                    format!("claim {id} is absent from claims.jsonl"),
                ));
            }
        }
        results += parsed.results.len();
        if write_index {
            for row in parsed.index_rows(&path) {
                serde_json::to_writer(&mut index, &row).map_err(|error| at(&article, error))?;
                index.push(b'\n');
            }
        }
    }
    if write_index {
        // Publish only complete, validated snapshots. Atomic replacement also keeps readers
        // from seeing a partial index when interrupted or when another generator is running.
        let target = root.join("index.jsonl");
        atomic::write(&target, &index).map_err(|error| at(&target, error))?;
    }
    Ok((dirs.len(), results))
}

fn article_dirs(root: &Path) -> Result<Vec<(PathBuf, String)>, String> {
    let mut dirs = Vec::new();
    for entry in fs::read_dir(root).map_err(|error| at(root, error))? {
        let entry = entry.map_err(|error| at(root, error))?;
        let path = entry.path();
        let file_type = entry.file_type().map_err(|error| at(&path, error))?;
        if file_type.is_symlink() && path.is_dir() {
            return Err(at(&path, "article directory must not be a symlink"));
        }
        if file_type.is_dir() {
            let name = entry
                .file_name()
                .into_string()
                .map_err(|_| at(&path, "article directory name must be UTF-8"))?;
            dirs.push((path, name));
        }
    }
    dirs.sort_unstable_by(|a, b| a.1.cmp(&b.1));
    Ok(dirs)
}

fn load_claims(dir: &Path) -> Result<HashSet<String>, String> {
    let mut ids = HashSet::new();
    let claims = read_article_file(dir, "claims.jsonl")?;
    for (line_no, line) in claims.lines().enumerate() {
        if line.trim().is_empty() {
            continue;
        }
        let location = format!("{}:{}", dir.join("claims.jsonl").display(), line_no + 1);
        let claim: Value =
            serde_json::from_str(line).map_err(|error| format!("{location}: {error}"))?;
        let id = claim["claim_id"]
            .as_str()
            .filter(|id| !id.trim().is_empty())
            .ok_or_else(|| format!("{location}: missing claim_id"))?;
        let data = claim["data"]
            .as_str()
            .filter(|data| !data.trim().is_empty())
            .ok_or_else(|| format!("{location}: claim {id} is missing a data file"))?;
        local_file(dir, data)
            .map_err(|reason| format!("{location}: claim {id} data file: {reason}"))?;
        if !ids.insert(id.to_owned()) {
            return Err(format!("{location}: duplicate claim {id}"));
        }
    }
    Ok(ids)
}

fn prose_claim_ids<'a>(body: &'a str, brackets: &'a Regex) -> impl Iterator<Item = &'a str> {
    brackets
        .captures_iter(body)
        .flat_map(|group| group.get(1).expect("bracket contents").as_str().split(','))
        .map(str::trim)
        .filter(|entry| {
            entry.strip_prefix('C').is_some_and(|digits| {
                !digits.is_empty() && digits.bytes().all(|byte| byte.is_ascii_digit())
            })
        })
}

fn read_article_file(dir: &Path, name: &str) -> Result<String, String> {
    let file = local_file(dir, name)?;
    fs::read_to_string(&file).map_err(|error| at(&dir.join(name), error))
}

fn local_file(dir: &Path, relative: &str) -> Result<PathBuf, String> {
    let path = Path::new(relative);
    if path
        .components()
        .any(|component| !matches!(component, Component::Normal(_)))
    {
        return Err(format!(
            "{relative}: expected an article-relative file path"
        ));
    }
    let path = dir.join(path);
    let base = dir.canonicalize().map_err(|error| at(dir, error))?;
    let target = path.canonicalize().map_err(|error| at(&path, error))?;
    if !target.starts_with(base) {
        return Err(at(&path, "file is outside the article directory"));
    }
    if !fs::metadata(&target)
        .map_err(|error| at(&path, error))?
        .is_file()
    {
        return Err(at(&path, "expected a regular file"));
    }
    Ok(target)
}

fn at(path: &Path, reason: impl std::fmt::Display) -> String {
    format!("{}: {reason}", path.display())
}
