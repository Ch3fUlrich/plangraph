//! The keeper Postgres schema version of a git revision, read from the frozen tree.
//!
//! A train that raises it cannot be started by a plain unit restart: the runtime role has no DDL and the
//! keeper refuses a database behind its code. The GO must carry `MIGRATION <version>` and the deploy goes
//! through `plangraph keeper-restart` (LE-KEEPER-SCHEMA-TRAIN-LANDS-WITHOUT-MIGRATION-FIELD).
use super::git;
use std::path::Path;

/// The file whose `MIGRATIONS` registry defines the version.
pub const SCHEMA_PATH: &str = "src/plangraph/keeper/schema_pg.py";

/// The version of the `MIGRATIONS = (...)` registry: the numbers of its direct `Migration(N, ...)` entries,
/// consecutive from 1. Strings, comments and the arguments of an entry are skipped, so a `Migration(` mention
/// elsewhere in the file does not count. A file with no registry is version 0; a gap, a repeat, an entry
/// that is not a `Migration(N, ...)` call or an unclosed registry is an error, never a guess.
pub fn parse_latest(text: &str) -> Result<u32, String> {
    let Some(start) = text
        .match_indices("MIGRATIONS = (")
        .map(|(at, m)| (at, at + m.len() - 1))
        .find(|(at, _)| text[..*at].chars().next_back().is_none_or(|c| c == '\n'))
        .map(|(_, open)| open)
    else {
        return Ok(0);
    };
    let (mut versions, mut depth, mut i) = (Vec::new(), 0u32, start);
    while i < text.len() {
        let rest = &text[i..];
        // Step by whole characters: a multi-byte one in a comment or string never splits.
        let width = rest.chars().next().map_or(1, char::len_utf8);
        i += match rest.as_bytes()[0] {
            b'#' | b'"' | b'\'' => literal_len(rest),
            b'(' if depth == 1 && entry_name(&text[..i]) => {
                let args = &rest[1..];
                let digits: String = args.chars().take_while(char::is_ascii_digit).collect();
                if digits.is_empty() || !args[digits.len()..].trim_start().starts_with(',') {
                    return Err("a registry entry is not Migration(N, ...)".into());
                }
                versions.push(digits.parse::<u32>().map_err(|e| e.to_string())?);
                depth += 1;
                1
            }
            b'(' if depth == 1 => return Err("a registry entry is not Migration(N, ...)".into()),
            b'(' => {
                depth += 1;
                1
            }
            b')' => {
                depth -= 1;
                if depth == 0 {
                    if versions.iter().zip(1u32..).any(|(v, n)| *v != n) {
                        return Err("keeper migration versions must be consecutive from 1".into());
                    }
                    return Ok(versions.len() as u32);
                }
                1
            }
            _ => width,
        };
    }
    Err("the MIGRATIONS registry is not closed".into())
}

/// Whether `before` ends in the bare name `Migration` (the call about to open).
fn entry_name(before: &str) -> bool {
    before.strip_suffix("Migration").is_some_and(|rest| {
        !rest
            .chars()
            .next_back()
            .is_some_and(|c| c.is_ascii_alphanumeric() || c == '_')
    })
}

/// Bytes of the comment or string literal that starts `text`, triple quotes included.
fn literal_len(text: &str) -> usize {
    let bytes = text.as_bytes();
    if bytes[0] == b'#' {
        return text.find('\n').unwrap_or(text.len());
    }
    let q = bytes[0] as char;
    let mark = if text.starts_with(&q.to_string().repeat(3)) {
        q.to_string().repeat(3)
    } else {
        q.to_string()
    };
    let mut i = mark.len();
    while i < text.len() && !text[i..].starts_with(&mark) {
        let width = if bytes[i] == b'\\' {
            1 + text[i + 1..].chars().next().map_or(0, char::len_utf8)
        } else {
            text[i..].chars().next().map_or(1, char::len_utf8)
        };
        i += width;
    }
    (i + mark.len()).min(text.len())
}

/// The keeper schema version at `rev`; 0 when the revision exists and has no schema file. A revision that
/// is not a commit of this repository is an error: a bad `--base` or `--head` must not read as version 0.
pub fn at(repo: &Path, rev: &str) -> Result<u32, String> {
    git::out(
        repo,
        &[
            "rev-parse",
            "--verify",
            "--quiet",
            &format!("{rev}^{{commit}}"),
        ],
    )
    .map_err(|_| format!("{rev} is not a commit of this repository"))?;
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
