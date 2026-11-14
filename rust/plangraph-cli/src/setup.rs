use crate::atomic;
use serde::Deserialize;
use std::path::{Component, Path};

pub const MANIFEST: &str = include_str!("../../../setup/manifest.toml");
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Manifest {
    pub files: Vec<Entry>,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Entry {
    #[serde(default)]
    pub platforms: Vec<String>,
    pub component: String,
    pub target: String,
    pub format: String,
    pub content: String,
}
#[derive(Clone, Copy, PartialEq)]
pub enum Mode {
    Apply,
    Check,
    DryRun,
}

pub fn run(home: &Path, manifest: &str, mode: Mode) -> (i32, Vec<String>) {
    let manifest: Manifest = match toml::from_str(manifest) {
        Ok(m) => m,
        Err(_) => return (2, vec!["setup manifest refused invalid manifest".into()]),
    };
    let mut rows = Vec::new();
    let mut changed = 0;
    let mut failed = 0;
    for entry in manifest.files {
        if !entry.platforms.is_empty()
            && !entry.platforms.iter().any(|os| os == std::env::consts::OS)
        {
            continue;
        }
        let target = Path::new(&entry.target);
        let safe = !entry.target.is_empty()
            && target
                .components()
                .all(|c| matches!(c, Component::Normal(_)));
        if !safe {
            rows.push(format!("{} target refused unsafe path", entry.component));
            failed += 1;
            continue;
        }
        let path = home.join(target);
        let result = apply(home, &path, &entry, mode);
        match result {
            Ok(status) => {
                if status != "skipped" {
                    changed += 1;
                }
                rows.push(format!(
                    "{} {} {} manifest",
                    entry.component, entry.target, status
                ));
            }
            Err(_) => {
                failed += 1;
                rows.push(format!(
                    "{} {} refused invalid target or write failure",
                    entry.component, entry.target
                ));
            }
        }
    }
    let code = if failed > 0 {
        if changed > 0 {
            3
        } else {
            2
        }
    } else if mode == Mode::Check && changed > 0 {
        1
    } else {
        0
    };
    (code, rows)
}

fn apply(home: &Path, path: &Path, entry: &Entry, mode: Mode) -> Result<&'static str, String> {
    // Reject symlinks from the target through home; ancestors above home may be links.
    for ancestor in path.ancestors() {
        if std::fs::symlink_metadata(ancestor).is_ok_and(|m| m.file_type().is_symlink()) {
            return Err("symlink ancestor".into());
        }
        if ancestor == home {
            break;
        }
    }
    let _lock = if mode == Mode::Apply {
        std::fs::create_dir_all(path.parent().unwrap()).map_err(|_| "mkdir")?;
        Some(atomic::lock(path).map_err(|_| "lock")?)
    } else {
        None
    };
    atomic::regular(path).map_err(|_| "target")?;
    let old = match std::fs::read(path) {
        Ok(bytes) => Some(bytes),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => None,
        Err(_) => return Err("read".into()),
    };
    let bytes = match entry.format.as_str() {
        "env" => {
            let text = old
                .as_deref()
                .map(std::str::from_utf8)
                .transpose()
                .map_err(|_| "UTF8")?
                .unwrap_or("");
            let values = crate::env::parse(text)?;
            let mut result = text.to_string();
            for (key, value) in crate::env::parse(&entry.content)? {
                if !values.contains_key(&key) {
                    if !result.is_empty() && !result.ends_with('\n') {
                        result.push('\n');
                    }
                    result.push_str(&format!("{key}={value}\n"));
                }
            }
            result.into_bytes()
        }
        "text" => entry.content.as_bytes().to_vec(),
        _ => return Err("unknown format".into()),
    };
    if old.as_deref() == Some(bytes.as_slice()) {
        return Ok("skipped");
    }
    if mode == Mode::Check {
        return Ok("drift");
    }
    if mode == Mode::DryRun {
        return Ok(if old.is_some() {
            "updated"
        } else {
            "installed"
        });
    }
    if let Some(old) = &old {
        atomic::backup(path, old).map_err(|_| "backup")?;
    }
    atomic::write(path, &bytes).map_err(|_| "write")?;
    Ok(if old.is_some() {
        "updated"
    } else {
        "installed"
    })
}
