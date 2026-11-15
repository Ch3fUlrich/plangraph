use super::fast_checks::Result;
use std::collections::BTreeSet;
use std::path::Path;

fn read_manifest(path: &Path) -> Result<toml::Value> {
    std::fs::read_to_string(path)
        .map_err(|e| format!("{}: {e}", path.display()))?
        .parse()
        .map_err(|e| format!("{}: {e}", path.display()))
}

// Follow local reverse dependencies, including crates in the separate worker workspace.
pub(super) fn packages(root: &Path, paths: &[String], full: bool) -> Result<Vec<(String, String)>> {
    let workspace = read_manifest(&root.join("rust/Cargo.toml"))?;
    let mut manifests: Vec<String> = workspace["workspace"]["members"]
        .as_array()
        .ok_or("workspace members missing")?
        .iter()
        .map(|m| format!("rust/{}/Cargo.toml", m.as_str().unwrap_or_default()))
        .collect();
    manifests.extend([
        "rust/worker/Cargo.toml".into(),
        "rust/spikes/temporal-spike/Cargo.toml".into(),
    ]);
    let mut crates = Vec::new();
    for manifest in manifests {
        let doc = read_manifest(&root.join(&manifest))?;
        let name = doc["package"]["name"]
            .as_str()
            .ok_or("package name missing")?
            .to_string();
        let mut deps = BTreeSet::new();
        let sections = std::iter::once(&doc).chain(
            doc.get("target")
                .and_then(toml::Value::as_table)
                .into_iter()
                .flat_map(|t| t.values()),
        );
        for section in sections {
            for table in ["dependencies", "dev-dependencies", "build-dependencies"] {
                if let Some(entries) = section.get(table).and_then(toml::Value::as_table) {
                    for (key, value) in entries {
                        if value.get("path").is_some() {
                            deps.insert(
                                value
                                    .get("package")
                                    .and_then(toml::Value::as_str)
                                    .unwrap_or(key)
                                    .to_string(),
                            );
                        }
                    }
                }
            }
        }
        crates.push((manifest, name, deps));
    }
    let shared = full
        || !paths.iter().any(|p| p.starts_with("rust/"))
        || paths.iter().any(|p| {
            matches!(
                p.as_str(),
                "rust/Cargo.toml" | "rust/Cargo.lock" | "rust-toolchain.toml"
            )
        });
    let mut selected: BTreeSet<String> = crates
        .iter()
        .filter(|(m, _, _)| {
            shared
                || paths
                    .iter()
                    .any(|p| p.starts_with(m.trim_end_matches("Cargo.toml")))
        })
        .map(|(_, name, _)| name.clone())
        .collect();
    loop {
        let before = selected.len();
        for (_, name, deps) in &crates {
            if !deps.is_disjoint(&selected) {
                selected.insert(name.clone());
            }
        }
        if selected.len() == before {
            break;
        }
    }
    Ok(crates
        .into_iter()
        .filter(|(_, name, _)| selected.contains(name))
        .map(|(m, n, _)| (m, n))
        .collect())
}
