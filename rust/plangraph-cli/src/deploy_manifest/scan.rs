//! What the tree ships: the unit files under `deploy/systemd` and the binary targets of the workspace
//! crates, found the way Cargo finds them, so the coverage test needs no build and no `cargo metadata`.
use std::collections::BTreeSet;
use std::fs;
use std::path::Path;

/// Every systemd unit type; a file of any of them under `deploy/` must be placed or excluded.
const UNIT_EXTENSIONS: [&str; 11] = [
    ".service",
    ".timer",
    ".socket",
    ".path",
    ".target",
    ".mount",
    ".automount",
    ".swap",
    ".slice",
    ".scope",
    ".device",
];

/// Unit files the tree ships, as coverage names them: a plain name for a file directly in `deploy/systemd`,
/// `<path under deploy/>` for any other (a unit that ships beside its tooling, at any depth; templates
/// included; `*.example` files and drop-in `.conf` files are not unit files).
pub fn unit_files(repo: &Path) -> Result<BTreeSet<String>, String> {
    let deploy = repo.join("deploy");
    let mut out = BTreeSet::new();
    walk(&deploy, "", &mut out)?;
    Ok(out)
}

fn walk(dir: &Path, rel: &str, out: &mut BTreeSet<String>) -> Result<(), String> {
    for entry in fs::read_dir(dir).map_err(|e| format!("{}: {e}", dir.display()))? {
        let entry = entry.map_err(|e| e.to_string())?;
        let name = entry.file_name().to_string_lossy().into_owned();
        let here = if rel.is_empty() {
            name.clone()
        } else {
            format!("{rel}/{name}")
        };
        // A symlinked directory is not followed: it could loop or leave the tree.
        let kind = entry.file_type().map_err(|e| e.to_string())?;
        if kind.is_dir() {
            walk(&entry.path(), &here, out)?;
        } else if entry.path().is_file() && UNIT_EXTENSIONS.iter().any(|ext| name.ends_with(ext)) {
            out.insert(here.strip_prefix("systemd/").unwrap_or(&here).to_owned());
        }
    }
    Ok(())
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
