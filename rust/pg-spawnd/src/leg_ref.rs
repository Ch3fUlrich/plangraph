//! Exact, role-aware references. Hints never select a route.
use std::path::Path;

#[derive(Clone, Debug, PartialEq, Eq, serde::Deserialize)]
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
pub(crate) fn configured_mode(role: &str) -> LegRefMode {
    let root = std::env::var_os("XDG_CONFIG_HOME")
        .map(std::path::PathBuf::from)
        .or_else(|| {
            std::env::var_os(if cfg!(windows) { "APPDATA" } else { "HOME" }).map(|p| {
                if cfg!(windows) {
                    p.into()
                } else {
                    std::path::PathBuf::from(p).join(".config")
                }
            })
        })
        .unwrap_or_default();
    mode(
        std::env::var("PLANGRAPH_RELIABILITY_LEG_REF")
            .ok()
            .as_deref(),
        &root.join("plangraph/reliability.json"),
        role,
    )
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
            static WARNING: std::sync::Once = std::sync::Once::new();
            WARNING.call_once(|| eprintln!("leg_ref: invalid switch; off"));
            LegRefMode::Off
        }
    }
}

#[cfg(test)]
#[path = "leg_ref_tests.rs"]
mod tests;

pub(crate) fn check_review_role(
    args: &crate::placement::Args,
) -> Result<(), crate::errors::ToolError> {
    let role = args
        .get("role")
        .and_then(|v| v.as_str())
        .unwrap_or("implement");
    let diff = args.get("diff_ref").is_some_and(|v| !v.is_null())
        || args
            .get("card")
            .and_then(|v| v.get("diff_ref"))
            .is_some_and(|v| !v.is_null());
    if role != "review" && diff {
        return Err(crate::placement::parameter(format!(
            "diff_ref requires role review; role {role} is refused"
        )));
    }
    Ok(())
}

pub(crate) fn admit_ref(
    args: &mut crate::placement::Args,
    catalog: &[LegRef],
    mode: LegRefMode,
) -> Result<(), crate::errors::ToolError> {
    check_review_role(args)?;
    if mode == LegRefMode::Off {
        return Ok(());
    }
    let role = args
        .get("role")
        .and_then(|v| v.as_str())
        .unwrap_or("implement");
    let resolve = || {
        let mut selected = None;
        for key in ["leg", "model"] {
            if let Some(value) = args.get(key) {
                let reference = value
                    .as_str()
                    .ok_or_else(|| crate::placement::parameter("invalid leg/reference"))?;
                let id = resolve_leg(reference, role, catalog).map_err(|error| {
                    let message = match error {
                        error @ (RefError::Unknown(_) | RefError::Ambiguous(_)) => {
                            let ambiguous = matches!(error, RefError::Ambiguous(_));
                            let (RefError::Unknown(ids) | RefError::Ambiguous(ids)) = error else {
                                unreachable!()
                            };
                            // Catalog strings are untrusted; never echo arbitrary ref or model text.
                            let hints: Vec<_> = ids
                                .into_iter()
                                .filter(|id| {
                                    id.len() <= 200
                                        && id.bytes().all(|b| {
                                            b.is_ascii_alphanumeric() || b"-_/.:".contains(&b)
                                        })
                                })
                                .collect();
                            if ambiguous {
                                format!("ambiguous reference; candidates: {}", hints.join(", "))
                            } else if hints.is_empty() {
                                "unknown leg/reference".into()
                            } else {
                                format!("unknown leg/reference; did-you-mean: {}", hints.join(", "))
                            }
                        }
                        _ => "unknown leg/reference or role-ineligible reference".into(),
                    };
                    crate::placement::parameter(message)
                })?;
                if selected.as_ref().is_some_and(|other| other != &id) {
                    return Err(crate::placement::parameter(
                        "conflicting leg/reference selectors",
                    ));
                }
                selected = Some(id);
            }
        }
        Ok(selected)
    };
    let result = resolve();
    if mode == LegRefMode::Warn {
        if result.is_err() {
            eprintln!("leg_ref: reference refused (observation only)");
        }
        return Ok(());
    }
    if let Some(id) = result? {
        args.insert("leg".into(), serde_json::Value::String(id));
        args.remove("model");
    }
    Ok(())
}

#[cfg(test)]
#[path = "leg_ref_admission_tests.rs"]
mod admission_tests;

pub(crate) trait LegCatalog {
    fn legs(&self) -> Result<Vec<LegRef>, crate::errors::ToolError>;
}
pub(crate) struct AdapterCatalog<'a> {
    state: &'a Path,
}
impl<'a> AdapterCatalog<'a> {
    pub(crate) fn new(state: &'a Path) -> Self {
        Self { state }
    }
}
impl LegCatalog for AdapterCatalog<'_> {
    fn legs(&self) -> Result<Vec<LegRef>, crate::errors::ToolError> {
        use std::sync::Mutex;
        use std::time::{Duration, Instant};
        type Cache = Option<(std::path::PathBuf, Instant, Vec<LegRef>)>;
        static CACHE: Mutex<Cache> = Mutex::new(None);
        let mut cached = CACHE.lock().unwrap_or_else(|e| e.into_inner());
        if let Some((path, at, rows)) = &*cached {
            if path == self.state && at.elapsed() < Duration::from_secs(60) {
                return Ok(rows.clone());
            }
        }
        let (code, bytes, _) = crate::adapter::Adapter(self.state)
            .run(&["catalog".into()], Duration::from_secs(30))?;
        let rows: Vec<LegRef> = serde_json::from_slice(&bytes)
            .ok()
            .filter(|_| code == 0)
            .ok_or_else(|| crate::placement::parameter("catalog unavailable"))?;
        *cached = Some((self.state.into(), Instant::now(), rows.clone()));
        Ok(rows)
    }
}
pub(crate) fn admit(
    catalog: &dyn LegCatalog,
    args: &crate::placement::Args,
) -> Result<crate::placement::Args, crate::errors::ToolError> {
    check_review_role(args)?;
    let role = args
        .get("role")
        .and_then(|v| v.as_str())
        .unwrap_or("implement");
    let mode = configured_mode(role);
    let mut admitted = args.clone();
    if mode != LegRefMode::Off && ["leg", "model"].iter().any(|k| args.contains_key(*k)) {
        // Reject malformed refs before even invoking the catalog adapter.
        let invalid = ["leg", "model"].iter().any(|k| {
            args.get(*k).is_some_and(|v| {
                v.as_str()
                    .is_none_or(|r| resolve_leg(r, role, &[]) == Err(RefError::Invalid))
            })
        });
        if invalid {
            admit_ref(&mut admitted, &[], mode)?;
        } else {
            let rows = catalog.legs();
            match rows {
                Ok(rows) => admit_ref(&mut admitted, &rows, mode)?,
                Err(_) if mode == LegRefMode::Warn => {
                    eprintln!("leg_ref: catalog unavailable (observation only)")
                }
                Err(error) => return Err(error),
            }
        }
    }
    Ok(admitted)
}
