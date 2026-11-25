//! `land-train require-migration`: a pull request that raises the keeper schema version says so.
//!
//! The required `ci-gate` job runs it on every full-tier pull request: when the schema version at the head is
//! above the merge base, the pull request body must hold the standalone line `MIGRATION <version>` (the line
//! `merge-batch push` writes, which the lander copies into the GO row). A rise without the line is refused;
//! no rise needs nothing. The versions come from the pinned revisions through the shared registry reader
//! (LE-KEEPER-SCHEMA-TRAIN-LANDS-WITHOUT-MIGRATION-FIELD).
use super::keeper_schema::rise;
use std::collections::BTreeMap;
use std::path::Path;
use std::process::Command;

/// Whether `body` has exactly one line that starts `MIGRATION ` and it is `MIGRATION <version>` in plain decimal.
fn body_names(body: &str, version: u32) -> bool {
    let lines: Vec<&str> = body
        .lines()
        .map(str::trim)
        .filter(|l| l.starts_with("MIGRATION "))
        .collect();
    lines == [format!("MIGRATION {version}")]
}

/// `Ok(line)` when no rise, or the rise is named in `body`; `Err(reason)` otherwise.
pub fn check(
    repo: &Path,
    base: &str,
    head: &str,
    body: &str,
    python: &dyn Fn() -> Command,
) -> Result<String, String> {
    match rise(repo, base, head, python)? {
        None => Ok("no keeper schema rise: nothing to require".into()),
        Some(version) if body_names(body, version) => Ok(format!(
            "keeper schema rises to {version}: the pull request names MIGRATION {version}"
        )),
        Some(version) => Err(format!(
            "keeper schema rises to {version}: the pull request body needs one line `MIGRATION {version}`, and the deploy is `plangraph keeper-restart CHECKOUT KEEPER_CONFIG MIGRATION_DSN_FILE`, never a plain restart"
        )),
    }
}

/// `land-train require-migration --repo DIR --base SHA --head SHA --body-file FILE`.
pub(super) fn run_cli(
    flags: &BTreeMap<String, String>,
    python: &dyn Fn() -> Command,
) -> Result<Vec<String>, String> {
    let need = |k: &str| flags.get(k).ok_or_else(|| format!("missing --{k}"));
    let file = need("body-file")?;
    let body = std::fs::read_to_string(file).map_err(|e| format!("{file}: {e}"))?;
    check(
        Path::new(need("repo")?),
        need("base")?,
        need("head")?,
        &body,
        python,
    )
    .map(|line| vec![line])
}
