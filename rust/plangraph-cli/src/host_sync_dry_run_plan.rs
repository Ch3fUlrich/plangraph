//! The planning step of the install dry run: unit sources into a `Needs` record.
use super::{Built, EnvFile, Needs};
use regex::Regex;
use std::collections::BTreeSet;
use std::path::Path;

const UNIT_SUFFIXES: [&str; 4] = [".service", ".timer", ".socket", ".path"];

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
            "WorkingDirectory" if !value.is_empty() => {
                let dir = value.trim_start_matches('-');
                let dir = if dir == "~" { "%h" } else { dir };
                needs.workdir = Some(dir.to_owned());
                if !value.starts_with(['-', '~']) {
                    needs.dirs.insert(value);
                }
            }
            "Unit" => timer_unit = Some(value),
            "ExecStart" if !value.is_empty() => {
                let command = value.trim_start_matches(['@', '-', ':', '+', '!']);
                if let Some(binary) = words(command).into_iter().next() {
                    needs.binaries.insert(binary);
                }
                needs.keys.extend(substituted_keys(command));
                if let [program, "state", "wait", rest @ ..] = &words(command)
                    .iter()
                    .map(String::as_str)
                    .collect::<Vec<_>>()[..]
                {
                    let named = program.rsplit('/').next() == Some("plangraph");
                    if let (true, Some(role)) = (named, rest.windows(2).find(|w| w[0] == "--role"))
                    {
                        needs.programs.insert(format!("state-wait:{}", role[1]));
                    }
                }
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
