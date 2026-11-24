//! The release install plan: the host binaries every workspace crate marks in its Cargo metadata.
use serde_json::Value;
use std::io;
use std::path::Path;
use std::process::Command;

/// One installed binary: the package that builds it and the binary name.
pub(super) type Binary = (String, String);

/// Marks live in each crate's manifest, so a new crate ships by marking itself:
/// `[package.metadata.plangraph] host-binaries = ["name"]`. Binaries that only some
/// hosts install (the keeper backup) go in `optional-host-binaries`.
const TABLE: &str = "plangraph";
const ALWAYS: &str = "host-binaries";
const OPTIONAL: &str = "optional-host-binaries";

pub(super) fn workspace_metadata(repo: &Path, cargo: &Path) -> io::Result<Value> {
    let output = Command::new(cargo)
        .current_dir(repo.join("rust"))
        .args(["metadata", "--no-deps", "--format-version", "1"])
        .output()?;
    if !output.status.success() {
        return Err(io::Error::other(format!(
            "cargo metadata: {}",
            String::from_utf8_lossy(&output.stderr).trim()
        )));
    }
    serde_json::from_slice(&output.stdout).map_err(io::Error::other)
}

/// Every marked binary, sorted. Fails when a mark names a binary the crate does not build,
/// so a typo or a removed binary never silently drops out of the install.
pub(super) fn plan(metadata: &Value, with_optional: bool) -> Result<Vec<Binary>, String> {
    let mut keys = vec![ALWAYS];
    if with_optional {
        keys.push(OPTIONAL);
    }
    let mut plan = Vec::new();
    let packages = metadata["packages"]
        .as_array()
        .map_or(&[][..], Vec::as_slice);
    for package in packages {
        let package_name = package["name"].as_str().unwrap_or_default();
        let built: Vec<&str> = package["targets"]
            .as_array()
            .map_or(&[][..], Vec::as_slice)
            .iter()
            .filter(|target| {
                target["kind"]
                    .as_array()
                    .is_some_and(|kinds| kinds.iter().any(|kind| kind == "bin"))
            })
            .filter_map(|target| target["name"].as_str())
            .collect();
        let marks = &package["metadata"][TABLE];
        for key in [ALWAYS, OPTIONAL] {
            for name in marks[key].as_array().map_or(&[][..], Vec::as_slice) {
                let name = name
                    .as_str()
                    .ok_or_else(|| format!("{package_name}: {key} holds a non-string entry"))?;
                if !built.contains(&name) {
                    return Err(format!(
                        "{package_name}: {key} names {name}, which the crate does not build"
                    ));
                }
                if keys.contains(&key) {
                    plan.push((package_name.to_owned(), name.to_owned()));
                }
            }
        }
    }
    plan.sort();
    plan.dedup();
    Ok(plan)
}

/// Fails when a binary marked host-binary in `metadata` is missing from `plan`.
pub(super) fn check(metadata: &Value, plan: &[Binary], with_optional: bool) -> Result<(), String> {
    let expected = self::plan(metadata, with_optional)?;
    let missing: Vec<String> = expected
        .iter()
        .filter(|binary| !plan.contains(binary))
        .map(|(package, name)| format!("{package}/{name}"))
        .collect();
    if missing.is_empty() {
        Ok(())
    } else {
        Err(format!(
            "host binaries missing from the install plan: {}",
            missing.join(", ")
        ))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn package(name: &str, bins: &[&str], metadata: Value) -> Value {
        let targets: Vec<Value> = bins
            .iter()
            .map(|bin| json!({"name": bin, "kind": ["bin"]}))
            .collect();
        json!({"name": name, "targets": targets, "metadata": metadata})
    }

    fn fixture() -> Value {
        json!({"packages": [
            package("pg-spawnd", &["pg-spawnd", "ci-temporal"], json!({"plangraph": {"host-binaries": ["pg-spawnd"]}})),
            package("plangraph-cli", &["plangraph", "host-sync"], json!({"plangraph": {"host-binaries": ["plangraph"]}})),
            package("pg-keeper-backup", &["pg-keeper-backup"], json!({"plangraph": {"optional-host-binaries": ["pg-keeper-backup"]}})),
            package("pg-unmarked", &["pg-unmarked"], Value::Null),
        ]})
    }

    fn names(plan: &[Binary]) -> Vec<&str> {
        plan.iter().map(|(_, name)| name.as_str()).collect()
    }

    #[test]
    fn plan_holds_only_marked_binaries() {
        let plan = plan(&fixture(), false).unwrap();
        assert_eq!(names(&plan), ["pg-spawnd", "plangraph"]);
        let with_keeper = super::plan(&fixture(), true).unwrap();
        assert_eq!(
            names(&with_keeper),
            ["pg-keeper-backup", "pg-spawnd", "plangraph"]
        );
    }

    #[test]
    fn a_new_crate_marking_itself_joins_the_plan() {
        let mut metadata = fixture();
        metadata["packages"].as_array_mut().unwrap().push(package(
            "pg-newcrate",
            &["pg-newcrate"],
            json!({"plangraph": {"host-binaries": ["pg-newcrate"]}}),
        ));
        let plan = plan(&metadata, false).unwrap();
        assert!(plan.contains(&("pg-newcrate".into(), "pg-newcrate".into())));
    }

    #[test]
    fn check_fails_on_a_marked_binary_left_out() {
        let metadata = fixture();
        let mut short = plan(&metadata, false).unwrap();
        check(&metadata, &short, false).unwrap();
        short.retain(|(_, name)| name != "plangraph");
        let error = check(&metadata, &short, false).unwrap_err();
        assert!(error.contains("plangraph-cli/plangraph"), "{error}");
    }

    #[test]
    fn a_mark_without_a_built_binary_is_refused() {
        let mut metadata = fixture();
        metadata["packages"].as_array_mut().unwrap().push(package(
            "pg-typo",
            &["pg-typo"],
            json!({"plangraph": {"host-binaries": ["pg-tpyo"]}}),
        ));
        let error = plan(&metadata, false).unwrap_err();
        assert!(error.contains("pg-tpyo"), "{error}");
    }

    #[test]
    fn the_workspace_marks_every_host_binary() {
        let repo = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
        let metadata = workspace_metadata(&repo, Path::new("cargo")).unwrap();
        let plan = plan(&metadata, true).unwrap();
        for name in [
            "pg-spawnd",
            "plangraph",
            "pg-disk-hygiene",
            "turn-budget-hook",
            "pg-telemetry",
            "pg-keeper-backup",
        ] {
            assert!(plan.iter().any(|(_, bin)| bin == name), "{name} not marked");
        }
        check(&metadata, &plan, true).unwrap();
    }
}
