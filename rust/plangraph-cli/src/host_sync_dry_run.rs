//! Host install dry run: what installing one unit family or one built binary needs on a host.
//!
//! `host-sync --install-dry-run [--registry FILE] [--repo DIR] HOST FAMILY` prints one
//! `MISSING <binary|file|env|path> NAME` line per absent thing and exits 1; a host that has everything
//! exits 0 with no output. Any other exit refuses the target (the deploy plan treats it as a refusal).
//!
//! The work has two steps so that one implementation serves local and ssh hosts: `plan` reads the unit
//! sources of the repository into a `Needs` record, and `evaluate` checks that record against a host's
//! files. A remote host receives the record on stdin of `--install-dry-run-here`.
use super::{is_executable, Env};
use pg_worker_core::hosts::{HostEntry, HostRegistry, Os};
use regex::Regex;
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet};
use std::ffi::OsString;
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};

/// `(kind, name)`; kind is `binary`, `file`, `env` or `path`.
pub(super) type Missing = (String, String);

const UNIT_SUFFIXES: [&str; 4] = [".service", ".timer", ".socket", ".path"];
/// Variables the service manager itself sets for a user service.
const MANAGER_KEYS: [&str; 5] = ["HOME", "USER", "LOGNAME", "SHELL", "PATH"];

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub(super) struct EnvFile {
    pub path: String,
    pub optional: bool,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub(super) struct Built {
    pub name: String,
    /// The repository root on the host, when the registry names one.
    pub repo: Option<String>,
}

/// What an install needs on the host. Paths may carry `%h` (the host's home) and `%%`.
#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
pub(super) struct Needs {
    /// Things already known absent where the sources are read (a unit file the repository lacks).
    pub absent: BTreeSet<Missing>,
    pub binaries: BTreeSet<String>,
    pub env_files: Vec<EnvFile>,
    pub environment: BTreeMap<String, String>,
    /// Variables the `ExecStart` lines read.
    pub keys: BTreeSet<String>,
    pub dirs: BTreeSet<String>,
    pub built: Option<Built>,
}

fn valid_family(family: &str) -> bool {
    !family.is_empty()
        && !family.starts_with(['-', '.'])
        && !family.contains("..")
        && family
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"@._:-".contains(&b))
}

/// The unit files a family names and the instance of a template; a bare name matches the unit files
/// of that name or its `pg-` and `plangraph-` forms.
fn unit_files(family: &str, units: &Path) -> Vec<(String, Option<String>)> {
    if let Some((stem, rest)) = family.split_once('@') {
        if let Some(suffix) = UNIT_SUFFIXES.iter().find(|s| rest.ends_with(**s)) {
            let instance = &rest[..rest.len() - suffix.len()];
            let template = format!("{stem}@{suffix}");
            return vec![(
                template,
                (!instance.is_empty()).then(|| instance.to_owned()),
            )];
        }
    }
    if UNIT_SUFFIXES.iter().any(|s| family.ends_with(s)) {
        return vec![(family.to_owned(), None)];
    }
    let mut found = Vec::new();
    for stem in [
        family.to_owned(),
        format!("pg-{family}"),
        format!("plangraph-{family}"),
    ] {
        for suffix in UNIT_SUFFIXES {
            for name in [format!("{stem}{suffix}"), format!("{stem}@{suffix}")] {
                if units.join(&name).is_file() {
                    found.push((name, None));
                }
            }
        }
    }
    if found.is_empty() {
        found.push((format!("{family}.service"), None));
    }
    found
}

fn logical_lines(text: &str) -> Vec<String> {
    let mut out = Vec::new();
    let mut open = String::new();
    for raw in text.lines() {
        let line = raw.trim_end();
        if let Some(head) = line.strip_suffix('\\') {
            open.push_str(head);
            open.push(' ');
            continue;
        }
        open.push_str(line);
        out.push(std::mem::take(&mut open).trim().to_owned());
    }
    out
}

/// Split `KEY=VALUE KEY2="a b"` into assignments.
fn assignments(text: &str) -> Vec<(String, String)> {
    let mut out = Vec::new();
    let (mut token, mut quoted, mut chars) = (String::new(), false, text.chars());
    let mut flush = |token: &mut String| {
        if let Some((k, v)) = token.split_once('=') {
            out.push((k.to_owned(), v.to_owned()));
        }
        token.clear();
    };
    while let Some(c) = chars.next() {
        match c {
            '"' => quoted = !quoted,
            '\\' if quoted => token.extend(chars.next()),
            c if c.is_whitespace() && !quoted => flush(&mut token),
            c => token.push(c),
        }
    }
    flush(&mut token);
    out
}

/// The words of a command line, quotes removed.
fn words(command: &str) -> Vec<String> {
    let (mut out, mut word, mut quote, mut open) = (Vec::new(), String::new(), None, false);
    let mut chars = command.chars();
    while let Some(c) = chars.next() {
        match (c, quote) {
            ('\\', _) => word.extend(chars.next()),
            ('\'' | '"', None) => (quote, open) = (Some(c), true),
            (c, Some(q)) if c == q => quote = None,
            (c, None) if c.is_whitespace() => {
                if open || !word.is_empty() {
                    out.push(std::mem::take(&mut word));
                }
                open = false;
            }
            (c, _) => word.push(c),
        }
    }
    if open || !word.is_empty() {
        out.push(word);
    }
    out
}

/// The variables systemd substitutes in a command line: `${NAME}` anywhere in a word and `$NAME` as a
/// whole word. Anything else, such as the locals of a `bash -c` script, belongs to the program.
fn substituted_keys(command: &str) -> BTreeSet<String> {
    let braced = Regex::new(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}").unwrap();
    let bare = Regex::new(r"^\$([A-Za-z_][A-Za-z0-9_]*)$").unwrap();
    let mut keys = BTreeSet::new();
    for word in words(command) {
        let word = word.replace("$$", "");
        keys.extend(braced.captures_iter(&word).map(|c| c[1].to_owned()));
        keys.extend(bare.captures(&word).map(|c| c[1].to_owned()));
    }
    keys
}

fn plan_unit(file: &str, instance: Option<&str>, units: &Path, follow: bool, needs: &mut Needs) {
    let Ok(text) = std::fs::read_to_string(units.join(file)) else {
        needs
            .absent
            .insert(("file".into(), format!("deploy/systemd/{file}")));
        return;
    };
    let fill = |s: &str| match instance {
        Some(i) => s.replace("%i", i).replace("%I", i),
        None => s.to_owned(),
    };
    let mut timer_unit = None;
    for line in logical_lines(&text) {
        let Some((key, value)) = line.split_once('=') else {
            continue;
        };
        let value = fill(value.trim());
        match key.trim() {
            "EnvironmentFile" if !value.is_empty() => {
                let (optional, path) = match value.strip_prefix('-') {
                    Some(path) => (true, path),
                    None => (false, value.as_str()),
                };
                needs.env_files.push(EnvFile {
                    path: path.to_owned(),
                    optional,
                });
            }
            "Environment" => needs.environment.extend(assignments(&value)),
            "WorkingDirectory" if !value.is_empty() && !value.starts_with(['-', '~']) => {
                needs.dirs.insert(value);
            }
            "Unit" => timer_unit = Some(value),
            "ExecStart" if !value.is_empty() => {
                let command = value.trim_start_matches(['@', '-', ':', '+', '!']);
                if let Some(binary) = words(command).into_iter().next() {
                    needs.binaries.insert(binary);
                }
                needs.keys.extend(substituted_keys(command));
            }
            _ => {}
        }
    }
    if let (true, Some(stem)) = (follow, file.strip_suffix(".timer")) {
        let service = timer_unit.unwrap_or_else(|| format!("{stem}.service"));
        plan_unit(&service, instance, units, false, needs);
    }
}

/// The needs of one family, read from the unit sources in `units` (the repository's `deploy/systemd`).
pub(super) fn plan(family: &str, units: &Path, repo: Option<&str>) -> Result<Needs, String> {
    if !valid_family(family) && !family.starts_with("bin:") {
        return Err(format!("unusable family {family:?}"));
    }
    let mut needs = Needs::default();
    if let Some(name) = family.strip_prefix("bin:") {
        if !valid_family(name) {
            return Err(format!("unusable binary name {name:?}"));
        }
        needs.built = Some(Built {
            name: name.to_owned(),
            repo: repo.map(str::to_owned),
        });
        needs.dirs.insert("%h/.local/bin".into());
        return Ok(needs);
    }
    for (file, instance) in unit_files(family, units) {
        plan_unit(&file, instance.as_deref(), units, true, &mut needs);
    }
    Ok(needs)
}

/// A path as printed: under the host's home as `~/...`, spaces as `%20`.
fn shown(path: &Path, home: &Path) -> String {
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
                load.unverifiable
                    .get_or_insert_with(|| raw.replace("%h", "~"));
            }
        }
    }
    load
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
    for binary in &needs.binaries {
        if binary.starts_with('$') {
            continue;
        }
        let Some(path) = expand(binary, home) else {
            continue;
        };
        if path.contains('/') {
            if !path.starts_with('/') {
                add("binary", binary.clone());
            } else if !is_executable(Path::new(&path)) {
                add("binary", shown(Path::new(&path), home));
            }
        } else {
            // A simple name is searched in the manager's directories and in the unit's own PATH.
            let own = base.vars.get("PATH").and_then(|p| expand(p, home));
            let mut dirs: Vec<String> = MANAGER_PATH.iter().map(|d| (*d).to_owned()).collect();
            dirs.extend(own.iter().flat_map(|p| p.split(':').map(str::to_owned)));
            if !dirs
                .iter()
                .any(|d| d.starts_with('/') && is_executable(&Path::new(d).join(&path)))
            {
                add("binary", binary.clone());
            }
        }
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
        let wanted: Vec<_> = needs
            .keys
            .iter()
            .filter(|k| !MANAGER_KEYS.contains(&k.as_str()))
            .collect();
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
            if let Some(path) = expand(value, home).filter(|p| p.starts_with('/')) {
                if !Path::new(&path).exists() {
                    add("path", shown(Path::new(&path), home));
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

fn print_result(missing: &BTreeSet<Missing>) -> i32 {
    for (kind, name) in missing {
        println!("MISSING {kind} {name}");
    }
    i32::from(!missing.is_empty())
}

fn usage() -> i32 {
    eprintln!("usage: host-sync --install-dry-run [--registry FILE] [--repo DIR] HOST FAMILY | --install-dry-run-here (needs on stdin)");
    2
}

/// `--install-dry-run-here`: the needs record on stdin, checked against this host.
pub(super) fn run_here() -> i32 {
    let mut raw = String::new();
    let parsed = std::io::stdin()
        .read_to_string(&mut raw)
        .map_err(|e| e.to_string())
        .and_then(|_| serde_json::from_str::<Needs>(&raw).map_err(|e| e.to_string()));
    let (Ok(needs), Some(home)) = (parsed, Env::from_process().home) else {
        eprintln!("host-sync: unreadable needs record or HOME is not set");
        return 2;
    };
    print_result(&evaluate(&needs, &home, &|k| std::env::var(k).ok()))
}

fn run_remote(entry: &HostEntry, route: &str, needs: &Needs) -> i32 {
    if route.starts_with('-') || route.contains(char::is_whitespace) {
        eprintln!("host-sync: unusable ssh route for {}", entry.id);
        return 2;
    }
    let spawned = Command::new("ssh")
        .args([
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=10",
            "--",
            route,
        ])
        .arg(".local/bin/plangraph host-sync --install-dry-run-here")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .spawn();
    let output = spawned.and_then(|mut child| {
        let body = serde_json::to_vec(needs).map_err(std::io::Error::other)?;
        child.stdin.take().expect("piped").write_all(&body)?;
        child.wait_with_output()
    });
    match output {
        Ok(out) if matches!(out.status.code(), Some(0 | 1)) => {
            print!("{}", String::from_utf8_lossy(&out.stdout));
            out.status.code().unwrap_or(2)
        }
        Ok(out) => {
            eprintln!("host-sync: dry run on {} failed: {}", entry.id, out.status);
            2
        }
        Err(e) => {
            eprintln!("host-sync: dry run on {} did not start: {e}", entry.id);
            2
        }
    }
}

pub(super) fn run(args: &[OsString]) -> i32 {
    let mut flags: BTreeMap<&str, PathBuf> = BTreeMap::new();
    let mut positional = Vec::new();
    let mut iter = args.iter();
    while let Some(arg) = iter.next() {
        match arg.to_str() {
            Some(flag @ ("--registry" | "--repo")) => match iter.next() {
                Some(value) => {
                    flags.insert(flag, PathBuf::from(value));
                }
                None => return usage(),
            },
            Some(value) if !value.starts_with("--") => positional.push(value.to_owned()),
            _ => return usage(),
        }
    }
    let [host, family] = positional.as_slice() else {
        return usage();
    };
    let env = Env::from_process();
    let Some(home) = env.home.clone() else {
        eprintln!("host-sync: HOME is not set");
        return 2;
    };
    let registry_path = flags.get("--registry").cloned().unwrap_or_else(|| {
        env.xdg_config_home
            .clone()
            .unwrap_or_else(|| home.join(".config"))
            .join("plangraph/hosts.json")
    });
    let registry = match HostRegistry::load(&registry_path) {
        Ok(registry) => registry,
        Err(e) => {
            eprintln!("host-sync: cannot load the hosts registry: {e}");
            return 2;
        }
    };
    let Some(entry) = registry.resolve(host) else {
        eprintln!("host-sync: {host} is not in the hosts registry");
        return 2;
    };
    if entry.os != Os::Linux {
        eprintln!(
            "host-sync: no install dry run for the {:?} host {}",
            entry.os, entry.id
        );
        return 2;
    }
    let repo = flags
        .get("--repo")
        .cloned()
        .unwrap_or_else(|| PathBuf::from("."));
    let local = entry.id == registry.self_host().id;
    let host_repo = if local {
        repo.canonicalize().ok().map(|p| p.display().to_string())
    } else {
        entry.repo("plangraph").map(|r| r.root.clone())
    };
    let needs = match plan(family, &repo.join("deploy/systemd"), host_repo.as_deref()) {
        Ok(needs) => needs,
        Err(e) => {
            eprintln!("host-sync: {e}");
            return 2;
        }
    };
    if local {
        return print_result(&evaluate(&needs, &home, &|k| std::env::var(k).ok()));
    }
    match entry.ssh_route() {
        Some(route) => run_remote(entry, &route, &needs),
        None => {
            eprintln!("host-sync: no route to {}", entry.id);
            2
        }
    }
}

#[cfg(test)]
#[path = "host_sync_dry_run_tests.rs"]
mod tests;
