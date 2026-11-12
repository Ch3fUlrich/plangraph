use super::{poll::parse, safe_name, Config};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeSet,
    fs::{self, File},
    io::{Read, Write},
    path::{Component, Path, PathBuf},
};

pub const FILE_MAX: u64 = 1024 * 1024;

pub fn read_json(path: &Path) -> Option<Value> {
    let mut bytes = Vec::new();
    File::open(path)
        .ok()?
        .take(FILE_MAX + 1)
        .read_to_end(&mut bytes)
        .ok()?;
    if bytes.len() as u64 > FILE_MAX {
        return None;
    }
    serde_json::from_slice(&bytes).ok()
}

pub fn atomic_write(path: &Path, bytes: &[u8]) -> std::io::Result<()> {
    if bytes.len() as u64 > FILE_MAX {
        return Err(std::io::Error::other("status too large"));
    }
    let parent = path.parent().unwrap();
    let mut file = tempfile::NamedTempFile::new_in(parent)?;
    file.write_all(bytes)?;
    match fs::metadata(path) {
        Ok(metadata) => file.as_file().set_permissions(metadata.permissions())?,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => (),
        Err(e) => return Err(e),
    }
    file.as_file().sync_all()?;
    file.persist(path).map_err(|e| e.error)?;
    #[cfg(unix)]
    File::open(parent)?.sync_all()?;
    Ok(())
}

pub fn successful(payload: &Value) -> bool {
    let response = &payload["tool_response"];
    !response.is_null()
        && response["is_error"] != true
        && response["interrupted"] != true
        && !response["exit_code"].as_i64().is_some_and(|n| n != 0)
}

pub fn changed_paths(payload: &Value, command: Option<&str>) -> Vec<std::path::PathBuf> {
    let cwd = payload["cwd"].as_str().unwrap_or("");
    if matches!(
        payload["tool_name"].as_str(),
        Some("Edit" | "Write" | "NotebookEdit")
    ) {
        return ["file_path", "notebook_path"]
            .iter()
            .filter_map(|key| payload["tool_input"][key].as_str())
            .filter_map(|path| absolute(path, cwd))
            .collect();
    }
    command.map(|s| mutation_paths(s, cwd)).unwrap_or_default()
}

fn hash(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}

fn absolute(path: &str, cwd: &str) -> Option<PathBuf> {
    let path = Path::new(path);
    let joined = if path.is_absolute() {
        path.to_owned()
    } else {
        Path::new(cwd).join(path)
    };
    if !joined.is_absolute() {
        return None;
    }
    let mut result = PathBuf::new();
    for part in joined.components() {
        match part {
            Component::ParentDir => {
                result.pop();
            }
            Component::CurDir => (),
            _ => result.push(part.as_os_str()),
        }
    }
    Some(result)
}

fn path_key(path: &Path) -> String {
    hash(path.as_os_str().as_encoded_bytes())
}

#[derive(Default, Debug, Deserialize, Serialize)]
#[serde(default)]
pub struct ReadState {
    paths: BTreeSet<String>,
    trees: BTreeSet<String>,
    pub output: Option<String>,
    pub retry: bool,
}
impl ReadState {
    pub fn affected_by(&self, paths: &[PathBuf]) -> bool {
        paths.iter().any(|path| {
            self.paths.contains(&path_key(path))
                || path.ancestors().any(|p| self.trees.contains(&path_key(p)))
        })
    }
}

pub fn read_scope(command: &str, cwd: &str) -> ReadState {
    let mut read = ReadState::default();
    let Some(shell) = parse(command) else {
        return read;
    };
    for c in shell.commands {
        if read.paths.len() + read.trees.len() >= 16 {
            break;
        }
        if c[0] == "git" || (c[0] == "ls" && c.len() == 1) {
            if let Some(path) = absolute(".", cwd) {
                read.trees.insert(path_key(&path));
            }
        } else if matches!(
            c[0].as_str(),
            "cat" | "head" | "tail" | "grep" | "rg" | "ls" | "stat" | "test" | "["
        ) {
            let skip = if matches!(c[0].as_str(), "grep" | "rg") {
                2
            } else {
                1
            };
            for arg in c
                .iter()
                .skip(skip)
                .filter(|s| !s.starts_with('-') && s.as_str() != "]")
                .take(16)
            {
                if read.paths.len() + read.trees.len() >= 16 {
                    break;
                }
                if let Some(path) = absolute(arg, cwd) {
                    read.paths.insert(path_key(&path));
                    if c[0] == "ls" {
                        read.trees.insert(path_key(&path));
                    }
                }
            }
        }
    }
    read
}

pub fn output_key(response: &Value) -> Option<String> {
    let output = if response.is_string() {
        response.clone()
    } else {
        let mut fields = serde_json::Map::new();
        for key in ["stdout", "stderr", "output", "content"] {
            if let Some(value) = response.get(key) {
                fields.insert(key.into(), value.clone());
            }
        }
        if fields.is_empty() {
            return None;
        }
        Value::Object(fields)
    };
    Some(hash(&serde_json::to_vec(&output).unwrap_or_default()))
}

fn mutation_paths(command: &str, cwd: &str) -> Vec<PathBuf> {
    let Some(shell) = parse(command) else {
        return vec![];
    };
    // A directory-changing compound command cannot be resolved from hook cwd.
    if shell.dynamic || shell.looped || shell.commands.iter().any(|c| c[0] == "cd") {
        return vec![];
    }
    let mut paths = Vec::new();
    for c in shell.commands {
        let mut args = &c[1..];
        let mut base = cwd;
        if c[0] == "git" {
            if args.first().is_some_and(|s| s == "-C") {
                let Some(dir) = args.get(1) else {
                    continue;
                };
                base = dir;
                args = &args[2..];
            }
            if args.first().is_some_and(|s| {
                matches!(
                    s.as_str(),
                    "add"
                        | "commit"
                        | "apply"
                        | "fetch"
                        | "pull"
                        | "merge"
                        | "rebase"
                        | "checkout"
                        | "switch"
                        | "reset"
                )
            }) {
                if let Some(path) = absolute(".", base) {
                    paths.push(path);
                }
            }
        } else if matches!(c[0].as_str(), "touch" | "mkdir" | "mv" | "rm" | "tee") {
            paths.extend(
                args.iter()
                    .filter(|s| !s.starts_with('-'))
                    .filter_map(|s| absolute(s, cwd)),
            );
        } else if matches!(c[0].as_str(), "cp" | "install") {
            // Sources are reads, not mutation evidence. Only infer a simple
            // destination; option operands need a real shell/argv model.
            if !args.iter().any(|s| s.starts_with('-')) {
                paths.extend(args.last().and_then(|s| absolute(s, cwd)));
            }
        }
        for (i, arg) in c.iter().enumerate() {
            if let Some((_, suffix)) = arg.split_once('\u{1}') {
                let target = suffix.trim_start_matches('\u{1}');
                let target = if target.is_empty() {
                    c.get(i + 1).map(String::as_str).unwrap_or("")
                } else {
                    target
                };
                if !target.is_empty() {
                    paths.extend(absolute(target, cwd));
                }
            }
        }
    }
    paths
}

pub fn resolve_name(config: &Config, id: &str) -> String {
    if let Some(name) = config.session_name.as_deref().filter(|s| safe_name(s)) {
        return name.into();
    }
    if let Ok(entries) = fs::read_dir(&config.launch_dir) {
        let mut paths: Vec<_> = entries.flatten().map(|e| e.path()).collect();
        paths.sort();
        for path in paths {
            if path.extension().is_none_or(|e| e != "json") {
                continue;
            }
            let Some(record) = read_json(&path) else {
                continue;
            };
            if record["session_id"] != id {
                continue;
            }
            if let Some(name) = record["name"].as_str().filter(|s| safe_name(s)) {
                return name.into();
            }
        }
    }
    id.into()
}
