//! The evaluation step of the install dry run: a `Needs` record against the files of this host.
use super::{is_executable, Missing, Needs};
use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};

/// Variables the service manager itself sets for a user service.
const MANAGER_KEYS: [&str; 5] = ["HOME", "USER", "LOGNAME", "SHELL", "PATH"];

/// A path as printed: under the host's home as `~/...`, spaces as `%20`.
pub(super) fn shown(path: &Path, home: &Path) -> String {
    let text = match path.strip_prefix(home) {
        Ok(rest) if rest.as_os_str().is_empty() => "~".to_owned(),
        Ok(rest) => format!("~/{}", rest.display()),
        Err(_) => path.display().to_string(),
    };
    text.replace(char::is_whitespace, "%20")
}

/// `%h` and `%%` expanded; `None` for any other specifier, which is not known without the service manager.
fn expand(text: &str, home: &Path) -> Option<String> {
    let mut out = String::new();
    let mut chars = text.chars();
    while let Some(c) = chars.next() {
        if c != '%' {
            out.push(c);
            continue;
        }
        match chars.next()? {
            'h' => out.push_str(&home.display().to_string()),
            '%' => out.push('%'),
            _ => return None,
        }
    }
    Some(out)
}

fn env_file_vars(path: &Path) -> Option<BTreeMap<String, String>> {
    let text = std::fs::read_to_string(path).ok()?;
    let mut vars = BTreeMap::new();
    for line in text.lines().map(str::trim) {
        if line.is_empty() || line.starts_with(['#', ';']) {
            continue;
        }
        let line = line.strip_prefix("export ").unwrap_or(line);
        let Some((key, value)) = line.split_once('=') else {
            continue;
        };
        let (key, value) = (key.trim(), value.trim());
        let unquoted = match value.as_bytes() {
            [b'"', .., b'"'] => {
                let mut out = String::new();
                let mut chars = value[1..value.len() - 1].chars();
                while let Some(c) = chars.next() {
                    out.extend(if c == '\\' { chars.next() } else { Some(c) });
                }
                out
            }
            [b'\'', .., b'\''] => value[1..value.len() - 1].to_owned(),
            _ => value.to_owned(),
        };
        vars.insert(key.to_owned(), unquoted);
    }
    Some(vars)
}

/// Where the service manager looks for a bare `ExecStart` name.
const MANAGER_PATH: [&str; 6] = [
    "/usr/local/sbin",
    "/usr/local/bin",
    "/usr/sbin",
    "/usr/bin",
    "/sbin",
    "/bin",
];

fn has_instance(path: &str) -> bool {
    path.contains("%i") || path.contains("%I")
}

/// The instances a template environment path has on this host (the file names that fit around `%i`), or
/// the finding that says why they cannot be listed.
fn instances(path: &str, home: &Path) -> Result<Vec<String>, Missing> {
    let shown_raw = path.replace("%h", "~");
    let at = path.find("%i").or_else(|| path.find("%I")).unwrap_or(0);
    let (head, tail) = (&path[..at], &path[at + 2..]);
    let (Some(slash), false) = (head.rfind('/'), tail.contains('/')) else {
        return Err(("file".into(), shown_raw));
    };
    let (Some(dir), Some(suffix)) = (expand(&head[..slash], home), expand(tail, home)) else {
        return Err(("file".into(), shown_raw));
    };
    let prefix = &head[slash + 1..];
    let Ok(entries) = std::fs::read_dir(&dir) else {
        return Err(("path".into(), shown(Path::new(&dir), home)));
    };
    let mut found: Vec<String> = entries
        .filter_map(|e| e.ok()?.file_name().into_string().ok())
        .filter(|n| {
            n.len() > prefix.len() + suffix.len() && n.starts_with(prefix) && n.ends_with(&suffix)
        })
        .map(|n| n[prefix.len()..n.len() - suffix.len()].to_owned())
        .collect();
    found.sort();
    Ok(found)
}

/// What the unit's environment holds for one instance (`None`: the unit is not a template instance).
struct EnvLoad {
    vars: BTreeMap<String, String>,
    /// The first environment file whose path cannot be expanded: its keys cannot be known.
    unverifiable: Option<String>,
    missing: Vec<Missing>,
}

fn load_env(needs: &Needs, home: &Path, instance: Option<&str>) -> EnvLoad {
    let mut load = EnvLoad {
        vars: needs.environment.clone(),
        unverifiable: None,
        missing: Vec::new(),
    };
    for file in &needs.env_files {
        let raw = match instance {
            Some(i) => file.path.replace("%i", i).replace("%I", i),
            None if has_instance(&file.path) => continue,
            None => file.path.clone(),
        };
        match expand(&raw, home) {
            Some(path) => match env_file_vars(Path::new(&path)) {
                Some(found) => load.vars.extend(found),
                None if file.optional && !Path::new(&path).exists() => {}
                None => load
                    .missing
                    .push(("file".into(), shown(Path::new(&path), home))),
            },
            None => {
                let shown_raw = raw.replace("%h", "~");
                // A mandatory file whose path cannot be expanded is missing whatever the command reads.
                if !file.optional {
                    load.missing.push(("file".into(), shown_raw.clone()));
                }
                load.unverifiable.get_or_insert(shown_raw);
            }
        }
    }
    load
}

/// Whether an environment value names a file or directory: it holds a `/` (and is no URL), or its key says
/// what it is. A bare word is a name, not a path.
fn path_like(key: &str, value: &str) -> bool {
    const SUFFIXES: [&str; 8] = [
        "_FILE",
        "_DIR",
        "_PATH",
        "_ROOT",
        "_REPO",
        "_PINS",
        "_WORKTREES",
        "_CONFIG",
    ];
    // A key that says it is a path keeps spaces in the value; prose with a slash in it is no path.
    !value.contains("://")
        && (SUFFIXES.iter().any(|s| key.ends_with(s))
            || (value.contains('/') && !value.contains(char::is_whitespace)))
}

/// Check `needs` against this host. `getenv` reads the host's process environment.
pub(super) fn evaluate(
    needs: &Needs,
    home: &Path,
    getenv: &dyn Fn(&str) -> Option<String>,
) -> BTreeSet<Missing> {
    let mut missing = needs.absent.clone();
    let mut add = |kind: &str, name: String| {
        missing.insert((kind.to_owned(), name));
    };
    let base = load_env(needs, home, None);
    for (kind, name) in &base.missing {
        add(kind, name.clone());
    }
    // A template's environment files are read for every instance present on the host.
    let mut names = BTreeSet::new();
    for file in needs.env_files.iter().filter(|f| has_instance(&f.path)) {
        match instances(&file.path, home) {
            Ok(found) if found.is_empty() && !file.optional => {
                add("file", file.path.replace("%h", "~"));
            }
            Ok(found) => names.extend(found),
            Err((kind, name)) => {
                if !file.optional {
                    add(&kind, name);
                }
            }
        }
    }
    let runs: Vec<Option<&str>> = if names.is_empty() {
        vec![None]
    } else {
        names.iter().map(|n| Some(n.as_str())).collect()
    };
    for run in runs {
        let load = if run.is_none() {
            EnvLoad {
                vars: base.vars.clone(),
                unverifiable: base.unverifiable.clone(),
                missing: Vec::new(),
            }
        } else {
            load_env(needs, home, run)
        };
        for (kind, name) in &load.missing {
            add(kind, name.clone());
        }
        // The service starts in its WorkingDirectory (the instance's, for a template), else in the home
        // directory: relative paths are read from there, never from the caller's directory.
        let start = match needs.workdir.as_deref() {
            None => Some(home.to_path_buf()),
            Some(dir) => {
                let dir =
                    run.map_or_else(|| dir.to_owned(), |i| dir.replace("%i", i).replace("%I", i));
                expand(&dir, home).map(PathBuf::from)
            }
        };
        let from_start = |p: &Path| -> Option<PathBuf> {
            if p.is_absolute() {
                Some(p.to_path_buf())
            } else {
                start.as_ref().map(|s| s.join(p))
            }
        };
        for binary in &needs.binaries {
            // An executable is resolved for the instance too: an unexpandable one is missing, not skipped.
            let raw = run.map_or_else(
                || binary.clone(),
                |i| binary.replace("%i", i).replace("%I", i),
            );
            if raw.starts_with('$') {
                continue;
            }
            let Some(path) = expand(&raw, home) else {
                add("binary", raw.replace("%h", "~"));
                continue;
            };
            if path.contains('/') {
                if !path.starts_with('/') {
                    add("binary", raw.clone());
                } else if !is_executable(Path::new(&path)) {
                    add("binary", shown(Path::new(&path), home));
                }
            } else {
                // A simple name is searched in the manager's directories and in the unit's own PATH.
                let own = load.vars.get("PATH").and_then(|p| expand(p, home));
                let mut dirs: Vec<String> = MANAGER_PATH.iter().map(|d| (*d).to_owned()).collect();
                dirs.extend(own.iter().flat_map(|p| p.split(':').map(str::to_owned)));
                if !dirs
                    .iter()
                    .any(|d| d.starts_with('/') && is_executable(&Path::new(d).join(&path)))
                {
                    add("binary", raw.clone());
                }
            }
        }
        let mut wanted: BTreeSet<&String> = needs
            .keys
            .iter()
            .filter(|k| !MANAGER_KEYS.contains(&k.as_str()))
            .collect();
        let shadow: Vec<&str> = needs
            .programs
            .iter()
            .filter_map(|p| p.strip_prefix("state-wait:"))
            .collect();
        let required: Vec<String> = if shadow.is_empty() {
            vec![]
        } else {
            crate::state_shadow::REQUIRED_ENV
                .iter()
                .map(|k| (*k).to_owned())
                .collect()
        };
        wanted.extend(required.iter());
        if let (Some(path), false) = (&load.unverifiable, wanted.is_empty()) {
            // The keys the command reads may live there: the host cannot be shown to have them.
            add("file", path.clone());
        }
        for key in wanted {
            let value = load.vars.get(key).filter(|v| !v.is_empty());
            let Some(value) = value else {
                if load.unverifiable.is_none() {
                    add("env", key.clone());
                }
                continue;
            };
            // A value that names a path is checked where the service would look: relative ones start at its
            // directory.
            if path_like(key, value) {
                match expand(value, home)
                    .map(PathBuf::from)
                    .and_then(|p| from_start(&p))
                {
                    Some(path) if !path.exists() => add("path", shown(&path, home)),
                    Some(_) => {}
                    None => add("path", value.clone()),
                }
            }
        }
        for role in shadow {
            let role = match (role, run) {
                ("%i", Some(instance)) => instance,
                ("%i", None) => continue,
                (role, _) => role,
            };
            let configured = load
                .vars
                .get(crate::state_shadow::CONFIG_ENV)
                .and_then(|v| expand(v, home));
            let named = crate::state_shadow::config_path(home, role, configured.as_deref());
            let Some(path) = from_start(&named) else {
                // The directory the service starts in cannot be told: neither can its configuration.
                add("file", shown(&named, home));
                continue;
            };
            // The runtime's own validation: a configuration `state wait` refuses is a missing file here.
            let reads = std::fs::read(&path)
                .map_err(|e| e.to_string())
                .and_then(|bytes| crate::state_shadow::read_paths(role, &bytes));
            match reads {
                Err(_) => add("file", shown(&path, home)),
                Ok(paths) => {
                    for read in paths {
                        match from_start(&read) {
                            Some(read) if !read.exists() => add("path", shown(&read, home)),
                            Some(_) => {}
                            None => add("file", shown(&path, home)),
                        }
                    }
                }
            }
        }
    }
    for dir in &needs.dirs {
        if let Some(path) = expand(dir, home) {
            if !Path::new(&path).is_dir() {
                add("path", shown(Path::new(&path), home));
            }
        }
    }
    if let Some(built) = &needs.built {
        let target = getenv("CARGO_TARGET_DIR").filter(|t| !t.is_empty());
        let rust = built.repo.as_ref().map(|r| Path::new(r).join("rust"));
        let target = match (target, rust) {
            (Some(t), Some(rust)) => Some(rust.join(t)),
            (Some(t), None) => Path::new(&t).is_absolute().then(|| PathBuf::from(t)),
            (None, Some(rust)) => Some(rust.join("target")),
            (None, None) => None,
        };
        match target {
            None => add("env", "CARGO_TARGET_DIR".into()),
            Some(target) => {
                let binary = target.join("release").join(format!(
                    "{}{}",
                    built.name,
                    std::env::consts::EXE_SUFFIX
                ));
                if !is_executable(&binary) {
                    add("binary", shown(&binary, home));
                }
            }
        }
    }
    missing
}
