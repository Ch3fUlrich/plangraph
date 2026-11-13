//! Read-only production path inventory, also used to protect referenced worktrees.
use crate::host_command;
use serde::Serialize;
use std::{
    collections::BTreeMap,
    fs, io,
    path::{Component, Path, PathBuf},
    process::Command,
    time::Duration,
};

#[derive(Clone, Debug, Serialize)]
pub struct Reference {
    pub kind: String,
    pub file: PathBuf,
    pub path: PathBuf,
}

pub fn normalize(path: &Path, home: &Path) -> PathBuf {
    let text = path
        .to_string_lossy()
        .replace("%h", &home.to_string_lossy())
        .replace("${HOME}", &home.to_string_lossy())
        .replace("$HOME", &home.to_string_lossy());
    let expanded = if text == "~" {
        home.to_path_buf()
    } else if let Some(p) = text.strip_prefix("~/") {
        home.join(p)
    } else {
        PathBuf::from(text)
    };
    let mut result = PathBuf::new();
    for part in expanded.components() {
        match part {
            Component::ParentDir => {
                result.pop();
            }
            Component::CurDir => {}
            p => result.push(p.as_os_str()),
        }
    }
    result
}

pub fn default_roots(home: &Path) -> Vec<PathBuf> {
    [
        home.join("code/plangraph"),
        home.join("code/plangraph-data"),
    ]
    .into_iter()
    .chain(
        [
            "/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc", "/run", "/var", "/dev",
        ]
        .map(PathBuf::from),
    )
    .collect()
}

pub fn forbidden(path: &Path) -> bool {
    let parts: Vec<_> = path
        .components()
        .map(|p| p.as_os_str().to_string_lossy().into_owned())
        .collect();
    parts.iter().any(|p| {
        p == "worktrees" || p.ends_with("-worktrees") || p == "plangraph-hooks" || p == "AutoOS"
    }) || parts
        .windows(2)
        .any(|p| p[0] == "code" && p[1] == "routing")
}

pub fn issues(refs: &[Reference], roots: &[PathBuf]) -> Vec<Reference> {
    refs.iter()
        .filter(|r| {
            let resolved = r.path.canonicalize().unwrap_or_else(|_| r.path.clone());
            forbidden(&r.path)
                || forbidden(&resolved)
                || !roots
                    .iter()
                    .any(|p| resolved.starts_with(p.canonicalize().unwrap_or_else(|_| p.clone())))
        })
        .cloned()
        .collect()
}

// Extract paths inside shell commands, assignments and colon-separated PYTHONPATH.
// No shell expansion or command execution is performed.
fn paths(value: &str, home: &Path) -> Vec<PathBuf> {
    let re = regex::Regex::new(r#"(?:/|~/|%h/|\$HOME/|\$\{HOME\}/)[^\s\"':;|&<>)]*"#).unwrap();
    let mut words = vec![];
    let mut word = String::new();
    let mut quote = None;
    let mut escaped = false;
    for c in value.chars() {
        if escaped {
            word.push(c);
            escaped = false;
        } else if c == '\\' {
            escaped = true;
        } else if quote == Some(c) {
            quote = None;
        } else if quote.is_none() && (c == '\'' || c == '"') {
            quote = Some(c);
        } else if quote.is_none() && c.is_whitespace() {
            if !word.is_empty() {
                words.push(std::mem::take(&mut word));
            }
        } else {
            word.push(c);
        }
    }
    if !word.is_empty() {
        words.push(word);
    }
    let mut result = vec![];
    for word in words {
        // Preserve whitespace in a quoted path or assignment. Shell command
        // strings (sh -c) still need scanning for each embedded absolute path.
        let value = word.split_once('=').map(|(_, v)| v).unwrap_or(&word);
        for part in value.split(':') {
            let part = part.trim_start_matches(['-', '+', '!', '@']);
            if ["/", "~/", "%h/", "$HOME/", "${HOME}/"]
                .iter()
                .any(|p| part.starts_with(p))
                && !part.contains(" /")
                && !part.contains([';', '|', '&', '\n'])
            {
                result.push(normalize(Path::new(part), home));
            } else {
                result.extend(
                    re.find_iter(part)
                        .map(|m| normalize(Path::new(m.as_str()), home)),
                );
            }
        }
    }
    result
}

fn add(refs: &mut Vec<Reference>, kind: &str, file: &Path, value: &str, home: &Path) {
    refs.extend(paths(value, home).into_iter().map(|path| Reference {
        kind: kind.into(),
        file: file.into(),
        path,
    }));
}

fn directives(file: &Path) -> io::Result<Vec<(String, String)>> {
    let text = fs::read_to_string(file)?;
    let mut result = vec![];
    let mut logical = String::new();
    let mut service = false;
    for line in text.lines() {
        let line = line.trim();
        if line.starts_with('#') || line.starts_with(';') {
            continue;
        }
        logical.push_str(line.trim_end_matches('\\'));
        if line.ends_with('\\') {
            logical.push(' ');
            continue;
        }
        if logical.starts_with('[') {
            service = logical == "[Service]";
        }
        if service {
            if let Some((key, value)) = logical.split_once('=') {
                if [
                    "ExecStart",
                    "ExecStartPre",
                    "WorkingDirectory",
                    "Environment",
                    "EnvironmentFile",
                ]
                .contains(&key.trim())
                {
                    result.push((key.trim().into(), value.trim().into()));
                }
            }
        }
        logical.clear();
    }
    Ok(result)
}

/// Read unit fragments and lexical drop-ins. Empty assignments reset prior values.
/// Inventory all environment paths conservatively, including environment-file contents.
pub fn unit_references(dir: &Path, home: &Path) -> io::Result<Vec<Reference>> {
    let mut refs = vec![];
    if !dir.exists() {
        return Ok(refs);
    }
    let mut units = BTreeMap::<String, Vec<PathBuf>>::new();
    for entry in fs::read_dir(dir)? {
        let path = entry?.path();
        let name = path.file_name().unwrap().to_string_lossy().into_owned();
        if name.ends_with(".d") && path.is_dir() {
            let mut drops = vec![];
            for e in fs::read_dir(&path)? {
                let p = e?.path();
                if p.extension().is_some_and(|x| x == "conf") {
                    drops.push(p);
                }
            }
            drops.sort();
            units
                .entry(name.trim_end_matches(".d").into())
                .or_default()
                .extend(drops);
        } else if path.is_file() && !name.starts_with('.') {
            units.entry(name).or_default().insert(0, path);
        }
    }
    for files in units.values() {
        let mut effective: BTreeMap<String, Vec<(PathBuf, String)>> = BTreeMap::new();
        for file in files {
            for (key, value) in directives(file)? {
                let values = effective.entry(key.clone()).or_default();
                if value.is_empty() || key == "WorkingDirectory" {
                    values.clear();
                }
                if !value.is_empty() {
                    values.push((file.clone(), value));
                }
            }
        }
        for (key, values) in effective {
            for (file, value) in values {
                add(&mut refs, &key, &file, &value, home);
                if key == "EnvironmentFile" {
                    for path in paths(&value, home) {
                        match fs::read_to_string(&path) {
                            Ok(text) => add(&mut refs, "EnvironmentFile-value", &path, &text, home),
                            Err(e)
                                if e.kind() == io::ErrorKind::NotFound
                                    && value.starts_with('-') => {}
                            Err(e) => return Err(e),
                        }
                    }
                }
            }
        }
    }
    Ok(refs)
}

fn optional_output(command: &mut Command) -> io::Result<String> {
    // Exit 1 means an unset hooksPath or no crontab; all other failures abort.
    host_command::isolate_git(command);
    let output = command.output()?;
    if !output.status.success() && output.status.code() != Some(1) {
        return Err(io::Error::other(format!(
            "inventory command {:?} failed: {}",
            command,
            String::from_utf8_lossy(&output.stderr)
        )));
    }
    if output.status.success() {
        String::from_utf8(output.stdout).map_err(io::Error::other)
    } else {
        Ok(String::new())
    }
}

pub fn inventory(dir: &Path, home: &Path, repos: &[PathBuf]) -> io::Result<Vec<Reference>> {
    let mut refs = unit_references(dir, home)?;
    let global = optional_output(Command::new("git").current_dir(home).args([
        "config",
        "--global",
        "--get",
        "core.hooksPath",
    ]))?;
    let hook = global.trim();
    if !hook.is_empty() {
        refs.push(Reference {
            kind: "hooksPath".into(),
            file: home.join(".gitconfig"),
            path: normalize(Path::new(hook), home),
        });
    }
    for repo in repos {
        let local = optional_output(Command::new("git").arg("-C").arg(repo).args([
            "config",
            "--local",
            "--get",
            "core.hooksPath",
        ]))?;
        if !local.trim().is_empty() {
            let path = normalize(Path::new(local.trim()), home);
            refs.push(Reference {
                kind: "hooksPath".into(),
                file: repo.join(".git/config"),
                path: if path.is_absolute() {
                    path
                } else {
                    repo.join(path)
                },
            });
        }
    }
    let cron = optional_output(Command::new("crontab").arg("-l"))?;
    for line in cron.lines().filter(|s| !s.trim_start().starts_with('#')) {
        add(&mut refs, "crontab", Path::new("crontab"), line, home);
    }
    Ok(refs)
}

pub fn command_output(command: &mut Command) -> io::Result<String> {
    String::from_utf8(host_command::output(command, Duration::from_secs(30))?)
        .map_err(io::Error::other)
}
