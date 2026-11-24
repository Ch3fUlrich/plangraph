//! The keeper Postgres schema version of a git revision, read from the frozen tree.
//!
//! A train that raises it cannot be started by a plain unit restart: the runtime role has no DDL and the
//! keeper refuses a database behind its code. The GO must carry `MIGRATION <version>` and the deploy goes
//! through `plangraph keeper-restart` (LE-KEEPER-SCHEMA-TRAIN-LANDS-WITHOUT-MIGRATION-FIELD).
use super::git;
use std::path::Path;

/// The file whose `MIGRATIONS` registry defines the version.
pub const SCHEMA_PATH: &str = "src/plangraph/keeper/schema_pg.py";

/// The version of a `MIGRATIONS` registry: the numbers of its `Migration(N, ...)` entries, consecutive
/// from 1. A file with no entry is version 0; a gap or a repeat is an error, never a guess.
pub fn parse_latest(text: &str) -> Result<u32, String> {
    let mut versions = Vec::new();
    for (at, _) in text.match_indices("Migration(") {
        let word_char = |c: char| c.is_ascii_alphanumeric() || c == '_';
        if text[..at].chars().next_back().is_some_and(word_char) {
            continue;
        }
        let rest = &text[at + "Migration(".len()..];
        let digits: String = rest.chars().take_while(char::is_ascii_digit).collect();
        if !digits.is_empty() && rest[digits.len()..].trim_start().starts_with(',') {
            versions.push(digits.parse::<u32>().map_err(|e| e.to_string())?);
        }
    }
    if versions.iter().zip(1u32..).any(|(v, n)| *v != n) {
        return Err("keeper migration versions must be consecutive from 1".into());
    }
    Ok(versions.len() as u32)
}

/// The keeper schema version at `rev`; 0 when the revision has no schema file.
pub fn at(repo: &Path, rev: &str) -> Result<u32, String> {
    let spec = format!("{rev}:{SCHEMA_PATH}");
    if !git::run(repo, None, &["cat-file", "-e", &spec])?
        .status
        .success()
    {
        return Ok(0);
    }
    let text = git::out(repo, &["show", &spec])?;
    parse_latest(&text).map_err(|e| format!("keeper migration version is unreadable at {rev}: {e}"))
}

/// The head version when `head` is above `base`; `None` when it is equal. A lower head is an error.
pub fn rise(repo: &Path, base: &str, head: &str) -> Result<Option<u32>, String> {
    let (before, after) = (at(repo, base)?, at(repo, head)?);
    if after < before {
        return Err(format!(
            "keeper migration regressed from {before} to {after}"
        ));
    }
    Ok((after > before).then_some(after))
}

/// `land-train keeper-schema --repo DIR --base SHA --head SHA`: one line the GO evidence copies.
pub(super) fn run_cli(
    flags: &std::collections::BTreeMap<String, String>,
) -> Result<Vec<String>, String> {
    let need = |k: &str| flags.get(k).ok_or_else(|| format!("missing --{k}"));
    let repo = Path::new(need("repo")?);
    let (base, head) = (at(repo, need("base")?)?, at(repo, need("head")?)?);
    Ok(vec![format!("keeper-schema base {base} head {head}")])
}
