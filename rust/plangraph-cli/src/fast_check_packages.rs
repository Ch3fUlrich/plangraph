use super::fast_checks::Result;
use std::collections::BTreeSet;
use std::path::Path;

/// One crate the fast checks build: its manifest, package name, and whether its own sources
/// changed (fmt and clippy run only on those; tests also run on every dependent).
#[derive(Debug, Clone, PartialEq, Eq)]
pub(super) struct Package {
    pub(super) manifest: String,
    pub(super) name: String,
    pub(super) lint: bool,
}

fn read_manifest(path: &Path) -> Result<toml::Value> {
    std::fs::read_to_string(path)
        .map_err(|e| format!("{}: {e}", path.display()))?
        .parse()
        .map_err(|e| format!("{}: {e}", path.display()))
}

/// Inputs every crate compiles or lints with: a change tests and lints the whole workspace.
fn workspace_wide(path: &str) -> bool {
    matches!(
        path,
        "rust/Cargo.toml" | "rust/Cargo.lock" | "rust-toolchain.toml"
    ) || path.starts_with("rust/test_support/")
        || path.starts_with("rust/.cargo/")
        || path
            .strip_prefix("rust/")
            .is_some_and(|name| !name.contains('/') && name.ends_with(".toml"))
}

/// Crates whose directory holds a changed path, plus their local reverse dependencies (also
/// across the separate worker workspace). `test_all` tests every crate (a non-Rust input Rust
/// tests read); `lint_all` also lints every crate (an input the selector cannot place). A diff
/// with no path under `rust/` is such a non-Rust input.
pub(super) fn packages(
    root: &Path,
    paths: &[String],
    test_all: bool,
    lint_all: bool,
) -> Result<Vec<Package>> {
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
    let wide = paths.iter().any(|p| workspace_wide(p));
    let lint_all = lint_all || wide;
    let test_all = test_all || wide || !paths.iter().any(|p| p.starts_with("rust/"));
    let changed = |manifest: &str| {
        let dir = manifest.trim_end_matches("Cargo.toml");
        paths.iter().any(|p| p.starts_with(dir))
    };
    let mut selected: BTreeSet<String> = crates
        .iter()
        .filter(|(m, _, _)| test_all || changed(m))
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
        .map(|(manifest, name, _)| Package {
            lint: lint_all || changed(&manifest),
            manifest,
            name,
        })
        .collect())
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A workspace of `core`, `app` (depends on core) and `leaf`, plus the worker and spike.
    fn fixture() -> tempfile::TempDir {
        let dir = tempfile::tempdir().unwrap();
        let write = |path: &str, text: &str| {
            let file = dir.path().join(path);
            std::fs::create_dir_all(file.parent().unwrap()).unwrap();
            std::fs::write(file, text).unwrap();
        };
        write(
            "rust/Cargo.toml",
            "[workspace]\nmembers = [\"core\", \"app\", \"leaf\"]\n",
        );
        write("rust/core/Cargo.toml", "[package]\nname = \"core\"\n");
        write(
            "rust/app/Cargo.toml",
            "[package]\nname = \"app\"\n[dependencies]\ncore = { path = \"../core\" }\n",
        );
        write("rust/leaf/Cargo.toml", "[package]\nname = \"leaf\"\n");
        write(
            "rust/worker/Cargo.toml",
            "[package]\nname = \"pg-worker\"\n",
        );
        write(
            "rust/spikes/temporal-spike/Cargo.toml",
            "[package]\nname = \"temporal-spike\"\n",
        );
        dir
    }

    fn pick(paths: &[&str], test_all: bool, lint_all: bool) -> Vec<(String, bool)> {
        let dir = fixture();
        let paths: Vec<String> = paths.iter().map(|p| (*p).to_string()).collect();
        packages(dir.path(), &paths, test_all, lint_all)
            .unwrap()
            .into_iter()
            .map(|p| (p.name, p.lint))
            .collect()
    }

    fn rows(rows: &[(&str, bool)]) -> Vec<(String, bool)> {
        rows.iter().map(|(n, l)| ((*n).to_string(), *l)).collect()
    }

    #[test]
    fn a_crate_change_lints_that_crate_and_tests_its_dependents() {
        assert_eq!(
            pick(&["rust/core/src/lib.rs"], false, false),
            rows(&[("core", true), ("app", false)])
        );
        assert_eq!(
            pick(&["rust/leaf/src/lib.rs"], false, false),
            rows(&[("leaf", true)])
        );
    }

    #[test]
    fn a_non_rust_input_tests_every_crate_and_lints_none() {
        let all = rows(&[
            ("core", false),
            ("app", false),
            ("leaf", false),
            ("pg-worker", false),
            ("temporal-spike", false),
        ]);
        assert_eq!(
            pick(&["src/plangraph/spawner/target.py"], false, false),
            all
        );
        // In a mixed diff only the changed crate is linted.
        let mixed = pick(&["docs/rust-hooks.md", "rust/leaf/src/lib.rs"], true, false);
        assert_eq!(mixed.iter().filter(|(_, lint)| *lint).count(), 1);
        assert_eq!(mixed.len(), 5);
    }

    #[test]
    fn workspace_wide_inputs_lint_and_test_every_crate() {
        for path in [
            "rust/Cargo.lock",
            "rust-toolchain.toml",
            "rust/test_support/python.rs",
            "rust/clippy.toml",
        ] {
            let picked = pick(&[path], false, false);
            assert_eq!(picked.len(), 5, "{path}");
            assert!(picked.iter().all(|(_, lint)| *lint), "{path}");
        }
        assert!(pick(&["README.md"], true, true)
            .iter()
            .all(|(_, lint)| *lint));
    }

    #[test]
    fn a_text_file_under_rust_outside_any_crate_selects_nothing() {
        assert!(pick(&["rust/TLS.md"], false, false).is_empty());
    }
}
