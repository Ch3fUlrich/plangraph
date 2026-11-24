//! What the keeper-schema evidence reads from a GO statement: the head-bound `MIGRATION` field.
//!
//! Both readers (this one and `merge_batch_land.require_migration_go`) take the same grammar: the
//! statement names one pull request and one full `GO-HEAD`, and its `;`-separated fields after the first
//! hold exactly one `MIGRATION <version>`, written as the plain decimal number and nothing else.
use regex::Regex;

/// `(pr, head)` named by a GO statement (`GO owner/name PR #9 ... GO-HEAD <40 hex>` or `GO #9 ...`), as
/// the keeper's GO binding reads it; `None` when it names no PR or not exactly one full head.
pub fn binding(statement: &str, repo: &str) -> Option<(u64, String)> {
    let project = repo.rsplit('/').next().unwrap_or(repo);
    let named = [repo, project].into_iter().find_map(|name| {
        let pattern = format!(
            r"^GO\s+(?:{}\s+PR\s+(?:#|`#)?|(?:`#|#))([0-9]+)`?(?:$|[\s,;:.])",
            regex::escape(name)
        );
        let found = Regex::new(&pattern).ok()?.captures(statement)?;
        let end = found.get(0)?.end();
        statement[..end]
            .matches('`')
            .count()
            .is_multiple_of(2)
            .then(|| found[1].parse::<u64>().ok())?
    })?;
    let token = Regex::new(r"GO-HEAD\s+`?([0-9a-fA-F]{40})").ok()?;
    let word = |c: char| c.is_alphanumeric() || c == '_';
    let heads: Vec<String> = token
        .captures_iter(statement)
        .filter(|c| {
            let (all, hex) = (c.get(0).unwrap(), c.get(1).unwrap());
            let before = statement[..all.start()].chars().next_back();
            let after = statement[hex.end()..].chars().next();
            !before.is_some_and(|b| word(b) || b == '-') && !after.is_some_and(word)
        })
        .map(|c| c[1].to_lowercase())
        .collect();
    match heads.as_slice() {
        [head] => Some((named, head.clone())),
        _ => None,
    }
}

/// The version of the one `MIGRATION <version>` field after the first `;` of `statement`; `None` when there
/// is no such field (a field of another name, `MIGRATION-NOTE staged`, is ignored). A second field, or a value that is not the plain decimal of a positive number
/// (`+3`, `03`, two spaces), is an error.
pub fn migration_field(statement: &str) -> Result<Option<u32>, String> {
    let fields: Vec<&str> = statement
        .split(';')
        .skip(1)
        .map(str::trim)
        .filter(|f| f.starts_with("MIGRATION "))
        .collect();
    match fields.as_slice() {
        [] => Ok(None),
        [one] => {
            let value = one.strip_prefix("MIGRATION ").unwrap_or("");
            let plain = !value.is_empty()
                && !value.starts_with('0')
                && value.bytes().all(|b| b.is_ascii_digit());
            value
                .parse::<u32>()
                .ok()
                .filter(|_| plain)
                .map(Some)
                .ok_or_else(|| format!("malformed field {one:?}"))
        }
        _ => Err("the GO statement has more than one MIGRATION field".into()),
    }
}

/// The migration of the GO row of this exact pull request and head: an error when the statement is bound to
/// another pull request or head, so a row for another head never supplies evidence for this one.
pub fn migration_for(
    statement: &str,
    repo: &str,
    pr: u64,
    head: &str,
) -> Result<Option<u32>, String> {
    match binding(statement, repo) {
        Some((n, h)) if n == pr && h == head.to_lowercase() => migration_field(statement),
        Some((n, h)) => Err(format!(
            "the GO statement is for #{n} at {h}, not #{pr} at {head}"
        )),
        None => Err("the GO statement names no single pull request and full GO-HEAD".into()),
    }
}
