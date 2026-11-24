//! What the tree ships: the unit files under `deploy/systemd` and the binary targets of the workspace
//! crates, found the way Cargo finds them, so the coverage test needs no build and no `cargo metadata`.
use std::collections::BTreeSet;
use std::fs;
use std::path::Path;

const UNIT_EXTENSIONS: [&str; 4] = [".service", ".timer", ".socket", ".path"];

/// Unit files the tree ships, as coverage names them: a plain name for `deploy/systemd`, `<dir>/<name>` for a
/// unit that ships beside its tooling in another directory directly under `deploy/` (templates included;
/// `*.example` files and drop-in directories are not unit files).
pub fn unit_files(repo: &Path) -> Result<BTreeSet<String>, String> {
    let deploy = repo.join("deploy");
    let mut out = BTreeSet::new();
    for entry in fs::read_dir(&deploy).map_err(|e| format!("{}: {e}", deploy.display()))? {
        let entry = entry.map_err(|e| e.to_string())?;
        let dir_name = entry.file_name().to_string_lossy().into_owned();
        if !entry.path().is_dir() {
            continue;
        }
        for file in fs::read_dir(entry.path()).map_err(|e| e.to_string())? {
            let file = file.map_err(|e| e.to_string())?;
            let name = file.file_name().to_string_lossy().into_owned();
            if file.path().is_file() && UNIT_EXTENSIONS.iter().any(|ext| name.ends_with(ext)) {
                out.insert(if dir_name == "systemd" {
                    name
                } else {
                    format!("{dir_name}/{name}")
                });
            }
        }
    }
    Ok(out)
}

fn read_toml(path: &Path) -> Result<toml::Table, String> {
    let text = fs::read_to_string(path).map_err(|e| format!("{}: {e}", path.display()))?;
    text.parse().map_err(|e| format!("{}: {e}", path.display()))
}

/// `(package, bin)` of every binary target of every crate in the `rust/` workspace.
pub fn bin_targets(repo: &Path) -> Result<BTreeSet<(String, String)>, String> {
    let rust = repo.join("rust");
    let workspace = read_toml(&rust.join("Cargo.toml"))?;
    let members = workspace["workspace"]["members"]
        .as_array()
        .ok_or("rust/Cargo.toml has no workspace members")?;
    let mut out = BTreeSet::new();
    for member in members {
        let dir = rust.join(member.as_str().ok_or("workspace member is not a path")?);
        let manifest = read_toml(&dir.join("Cargo.toml"))?;
        let package = manifest["package"]["name"]
            .as_str()
            .ok_or_else(|| format!("{}: no package name", dir.display()))?
            .to_owned();
        let mut paths = BTreeSet::new();
        let explicit = manifest
            .get("bin")
            .and_then(|b| b.as_array())
            .cloned()
            .unwrap_or_default();
        for bin in &explicit {
            let name = bin["name"]
                .as_str()
                .ok_or_else(|| format!("{package}: [[bin]] without name"))?;
            let path = bin
                .get("path")
                .and_then(|p| p.as_str())
                .unwrap_or("src/main.rs");
            paths.insert(path.to_owned());
            out.insert((package.clone(), name.to_owned()));
        }
        let auto = manifest
            .get("package")
            .and_then(|p| p.get("autobins"))
            .and_then(|a| a.as_bool())
            .unwrap_or(true);
        if !auto {
            continue;
        }
        if dir.join("src/main.rs").is_file() && !paths.contains("src/main.rs") {
            out.insert((package.clone(), package.clone()));
        }
        let bin_dir = dir.join("src/bin");
        if let Ok(read) = fs::read_dir(&bin_dir) {
            for entry in read {
                let entry = entry.map_err(|e| e.to_string())?;
                let path = entry.path();
                let name = entry.file_name().to_string_lossy().into_owned();
                let rel = format!("src/bin/{name}");
                if path.is_file() && name.ends_with(".rs") && !paths.contains(&rel) {
                    out.insert((package.clone(), name.trim_end_matches(".rs").to_owned()));
                } else if path.is_dir()
                    && path.join("main.rs").is_file()
                    && !paths.contains(&format!("{rel}/main.rs"))
                {
                    out.insert((package.clone(), name));
                }
            }
        }
    }
    Ok(out)
}
