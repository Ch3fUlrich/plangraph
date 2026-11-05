//! Exact, role-aware references. Hints never select a route.
use std::path::Path;

#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct LegRef {
    pub id: String,
    pub model: String,
    pub roles: Vec<String>,
}
#[derive(Debug, PartialEq, Eq)]
pub(crate) enum RefError {
    Invalid,
    Unknown(Vec<String>),
    Ambiguous(Vec<String>),
    Role,
}
fn distance(a: &str, b: &str) -> usize {
    let b = b.to_ascii_lowercase();
    let mut row: Vec<usize> = (0..=b.len()).collect();
    for (i, x) in a.to_ascii_lowercase().bytes().enumerate() {
        let mut prev = row[0];
        row[0] = i + 1;
        for (j, y) in b.bytes().enumerate() {
            let old = row[j + 1];
            row[j + 1] = (prev + usize::from(x != y)).min(old + 1).min(row[j] + 1);
            prev = old;
        }
    }
    row[b.len()]
}
pub(crate) fn resolve_leg(
    reference: &str,
    role: &str,
    catalog: &[LegRef],
) -> Result<String, RefError> {
    if reference.is_empty()
        || reference.len() > 200
        || reference.starts_with('-')
        || reference
            .chars()
            .any(|c| c.is_control() || c.is_whitespace())
    {
        return Err(RefError::Invalid);
    }
    if !["implement", "review", "orchestrate"].contains(&role) {
        return Err(RefError::Role);
    }
    let eligible = |leg: &&LegRef| leg.roles.iter().any(|r| r == role);
    let exact: Vec<_> = catalog.iter().filter(|l| l.id == reference).collect();
    if !exact.is_empty() {
        return if exact.iter().copied().any(|l| eligible(&l)) {
            Ok(reference.into())
        } else {
            Err(RefError::Role)
        };
    }
    let mut matches: Vec<_> = catalog
        .iter()
        .filter(eligible)
        .filter(|l| l.model == reference)
        .map(|l| l.id.clone())
        .collect();
    matches.sort();
    matches.dedup();
    if matches.len() == 1 {
        return Ok(matches.remove(0));
    }
    if !matches.is_empty() {
        matches.truncate(3);
        return Err(RefError::Ambiguous(matches));
    }
    if catalog.iter().any(|l| l.model == reference) {
        return Err(RefError::Role);
    }
    let mut hints: Vec<_> = catalog
        .iter()
        .filter(eligible)
        .map(|l| {
            (
                distance(reference, &l.id).min(distance(reference, &l.model)),
                l.id.clone(),
            )
        })
        .filter(|(d, _)| *d <= 3)
        .collect();
    hints.sort();
    let mut ids = Vec::new();
    for (_, id) in hints {
        if !ids.contains(&id) {
            ids.push(id);
        }
        if ids.len() == 3 {
            break;
        }
    }
    Err(RefError::Unknown(ids))
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum LegRefMode {
    Off,
    Warn,
    On,
}
// No process environment mutation; callers inject lookup and platform config path.
pub(crate) fn mode(env: Option<&str>, path: &Path, role: &str) -> LegRefMode {
    let file = std::fs::read(path)
        .ok()
        .and_then(|v| serde_json::from_slice::<serde_json::Value>(&v).ok());
    let configured = file.as_ref().and_then(|v| v.get("leg_ref"));
    let configured =
        configured.and_then(|v| v.as_str().or_else(|| v.get(role).and_then(|v| v.as_str())));
    match env.or(configured).unwrap_or("off") {
        "on" => LegRefMode::On,
        "warn" | "shadow" => LegRefMode::Warn,
        "off" => LegRefMode::Off,
        _ => {
            eprintln!("leg_ref: invalid switch; off");
            LegRefMode::Off
        }
    }
}

#[cfg(test)]
#[path = "leg_ref_tests.rs"]
mod tests;
