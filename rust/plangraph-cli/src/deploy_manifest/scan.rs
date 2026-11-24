//! What the tree ships: the tracked unit files under `deploy/` and the binary targets of the workspace
//! crates, found the way Cargo finds them, so the coverage test needs no build and no `cargo metadata`.
use std::collections::BTreeSet;
use std::fs;
use std::path::Path;
use std::process::Command;

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

/// Unit files the tree ships, as coverage names them: the path under `deploy/` (`systemd/foo.service`,
/// `pganchor/pganchor.socket`; any depth, templates included; `*.example` files and drop-in `.conf`
/// files are not unit files). Only files Git tracks count: an untracked file is not shipped, so it
/// neither fails the gate nor satisfies an entry. A tracked file missing from the working tree is an error.
pub fn unit_files(repo: &Path) -> Result<BTreeSet<String>, String> {
    let mut out = BTreeSet::new();
    for path in tracked(repo, "deploy")? {
        let Some(rel) = path.strip_prefix("deploy/") else {
            continue;
        };
        if !UNIT_EXTENSIONS.iter().any(|ext| rel.ends_with(ext)) {
            continue;
        }
        if !repo.join(&path).is_file() {
            return Err(format!(
                "{path} is tracked but missing from the working tree"
            ));
        }
        out.insert(rel.to_owned());
    }
    Ok(out)
}

/// The paths Git tracks under `sub` of `repo`, relative to `repo`.
fn tracked(repo: &Path, sub: &str) -> Result<Vec<String>, String> {
    // A hook or test runner may export GIT_DIR or GIT_INDEX_FILE; they would point at another repository.
    let output = Command::new("git")
        .arg("-C")
        .arg(repo)
        .args(["ls-files", "-z", "--", sub])
        .env_remove("GIT_DIR")
        .env_remove("GIT_INDEX_FILE")
        .env_remove("GIT_WORK_TREE")
        .output()
        .map_err(|e| format!("git ls-files: {e}"))?;
    if !output.status.success() {
        return Err(format!(
            "git ls-files in {}: {}",
            repo.display(),
            String::from_utf8_lossy(&output.stderr).trim()
        ));
    }
    Ok(output
        .stdout
        .split(|b| *b == 0)
        .filter(|p| !p.is_empty())
        .map(|p| String::from_utf8_lossy(p).into_owned())
        .collect())
}

/// The unit settings that run a program.
const EXEC_KEYS: [&str; 7] = [
    "ExecStart",
    "ExecStartPre",
    "ExecStartPost",
    "ExecCondition",
    "ExecReload",
    "ExecStop",
    "ExecStopPost",
];

/// Where the tracked unit files run a binary from: `(bin, path)` of the program of every `Exec*` line under `/usr/local/bin` or `%h/.local/bin` (`%h` is written `~`). Reads the
/// files only; nothing here asks systemd.
pub fn consumer_paths(
    repo: &Path,
    unit_files: &BTreeSet<String>,
) -> Result<BTreeSet<(String, String)>, String> {
    let mut out = BTreeSet::new();
    for unit in unit_files {
        let path = repo.join("deploy").join(unit);
        let text = fs::read_to_string(&path).map_err(|e| format!("{}: {e}", path.display()))?;
        for line in text.lines() {
            let Some((key, value)) = line.split_once('=') else {
                continue;
            };
            if !EXEC_KEYS.contains(&key.trim()) {
                continue;
            }
            let program = value
                .trim_start_matches(['-', '+', '!', '@', ' '])
                .split_whitespace()
                .next()
                .unwrap_or("");
            let program = program.replacen("%h", "~", 1);
            for dir in ["/usr/local/bin/", "~/.local/bin/"] {
                if let Some(bin) = program.strip_prefix(dir) {
                    if !bin.is_empty() && !bin.contains('/') {
                        out.insert((bin.to_owned(), program.clone()));
                    }
                }
            }
        }
    }
    Ok(out)
}

/// The env files the tracked unit files read: `(unit key, file name)` of every `EnvironmentFile=` line
/// under `%h/.config/plangraph/` or `/etc/plangraph/`, the file name relative to that directory with the
/// leading `-` (optional file) dropped and `%i` kept as written.
pub fn unit_env_files(
    repo: &Path,
    unit_files: &BTreeSet<String>,
) -> Result<BTreeSet<(String, String)>, String> {
    let mut out = BTreeSet::new();
    for unit in unit_files {
        let path = repo.join("deploy").join(unit);
        let text = fs::read_to_string(&path).map_err(|e| format!("{}: {e}", path.display()))?;
        for line in text.lines() {
            let Some(("EnvironmentFile", value)) =
                line.split_once('=').map(|(k, v)| (k.trim(), v.trim()))
            else {
                continue;
            };
            let value = value.trim_start_matches('-');
            for dir in ["%h/.config/plangraph/", "/etc/plangraph/"] {
                if let Some(name) = value.strip_prefix(dir) {
                    out.insert((unit.clone(), name.to_owned()));
                }
            }
        }
    }
    Ok(out)
}

/// The workspace member directories under `rust/`: Cargo's `members` with `*` and `?` expanded per path
/// component, minus `exclude`. A pattern that matches nothing is an error, as it is for Cargo.
fn members(rust: &Path, workspace: &toml::Table) -> Result<Vec<String>, String> {
    let list = |key: &str| -> Vec<String> {
        workspace["workspace"]
            .get(key)
            .and_then(|m| m.as_array())
            .map(|a| {
                a.iter()
                    .filter_map(|v| v.as_str().map(str::to_owned))
                    .collect()
            })
            .unwrap_or_default()
    };
    let excluded: BTreeSet<String> = list("exclude").into_iter().collect();
    let mut out = BTreeSet::new();
    for pattern in list("members") {
        let mut found = vec![String::new()];
        for part in pattern.split('/').filter(|p| !p.is_empty() && *p != ".") {
            let mut next = Vec::new();
            for base in &found {
                if part.contains(['*', '?', '[']) {
                    let dir = rust.join(base);
                    for entry in
                        fs::read_dir(&dir).map_err(|e| format!("{}: {e}", dir.display()))?
                    {
                        let entry = entry.map_err(|e| e.to_string())?;
                        let name = entry.file_name().to_string_lossy().into_owned();
                        if entry.path().is_dir() && wildcard(part, &name) {
                            next.push(join(base, &name));
                        }
                    }
                } else {
                    next.push(join(base, part));
                }
            }
            found = next;
        }
        found.retain(|m| rust.join(m).join("Cargo.toml").is_file());
        if found.is_empty() {
            return Err(format!("workspace member {pattern:?} matches no crate"));
        }
        out.extend(found);
    }
    Ok(out.into_iter().filter(|m| !excluded.contains(m)).collect())
}

fn join(base: &str, name: &str) -> String {
    if base.is_empty() {
        name.to_owned()
    } else {
        format!("{base}/{name}")
    }
}

/// Shell-style match of one path component: `*` any run, `?` one character; `[` is not supported.
fn wildcard(pattern: &str, name: &str) -> bool {
    fn go(p: &[char], n: &[char]) -> bool {
        match (p.first(), n.first()) {
            (None, None) => true,
            (Some('*'), _) => go(&p[1..], n) || (!n.is_empty() && go(p, &n[1..])),
            (Some('?'), Some(_)) => go(&p[1..], &n[1..]),
            (Some(a), Some(b)) if a == b => go(&p[1..], &n[1..]),
            _ => false,
        }
    }
    go(
        &pattern.chars().collect::<Vec<_>>(),
        &name.chars().collect::<Vec<_>>(),
    )
}

fn read_toml(path: &Path) -> Result<toml::Table, String> {
    let text = fs::read_to_string(path).map_err(|e| format!("{}: {e}", path.display()))?;
    text.parse().map_err(|e| format!("{}: {e}", path.display()))
}

/// `(package, bin)` of every binary target of every crate in the `rust/` workspace.
pub fn bin_targets(repo: &Path) -> Result<BTreeSet<(String, String)>, String> {
    let rust = repo.join("rust");
    let workspace = read_toml(&rust.join("Cargo.toml"))?;
    let mut out = BTreeSet::new();
    for member in members(&rust, &workspace)? {
        let dir = rust.join(member);
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
