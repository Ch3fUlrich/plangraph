//! What the tree ships: the tracked unit files under `deploy/` and the binary targets of the workspace
//! crates, as Git and Cargo list them. Nothing here asks systemd.
use std::collections::BTreeSet;
use std::fs;
use std::path::Path;
use std::process::Command;

/// Every systemd unit type; a file of any of them under `deploy/` must be placed or excluded, and so
/// must its template form (`<name>.in`, rendered by an installer). `tools/ci_select.py` selects Rust CI
/// for the same suffixes; `tests/test_ci_select_rust_inputs.py` compares the two lists.
pub const UNIT_EXTENSIONS: [&str; 11] = [
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

/// `true` for a unit file name or its `.in` template (`*.example` files and drop-in `.conf` are not units).
pub fn is_unit_name(name: &str) -> bool {
    let name = name.strip_suffix(".in").unwrap_or(name);
    UNIT_EXTENSIONS.iter().any(|ext| name.ends_with(ext))
}

/// Unit files the tree ships, as coverage names them: the path under `deploy/` (`systemd/foo.service`,
/// `pganchor/pganchor.socket`, `ctx-watchdog/ctx-watchdog.timer.in`; any depth, templates included).
/// Only files Git tracks count: an untracked file is not shipped, so it neither fails the gate nor
/// satisfies an entry. A tracked file missing from the working tree is an error.
pub fn unit_files(repo: &Path) -> Result<BTreeSet<String>, String> {
    let mut out = BTreeSet::new();
    for path in tracked(repo, "deploy")? {
        let Some(rel) = path.strip_prefix("deploy/") else {
            continue;
        };
        if !is_unit_name(rel) {
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
pub fn tracked(repo: &Path, sub: &str) -> Result<Vec<String>, String> {
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

/// The program of an `Exec*` value, read as systemd reads the first word: whitespace ends it unless
/// quoted, `"` and `'` quote (also inside a word), C escapes decode into bytes (`\x2f`, `\057`, `\s`, `\"`,
/// `\u002f`, `\U0000002f`; `\xc3\xa9` is the two bytes of one character), and only then do the prefix
/// characters (`@ - : + !`) come off the decoded word.
fn first_word(value: &str) -> String {
    let mut word: Vec<u8> = Vec::new();
    let mut quote: Option<char> = None;
    let mut chars = value.trim_start().chars().peekable();
    while let Some(c) = chars.next() {
        match c {
            '\\' => word.extend(unescape(&mut chars)),
            c if quote == Some(c) => quote = None,
            '"' | '\'' if quote.is_none() => quote = Some(c),
            c if c.is_whitespace() && quote.is_none() => break,
            c => word.extend(c.to_string().into_bytes()),
        }
    }
    let word = String::from_utf8_lossy(&word);
    word.trim_start_matches(['@', '-', ':', '+', '!'])
        .to_owned()
}

type Chars<'a> = std::iter::Peekable<std::str::Chars<'a>>;

/// Up to `max` digits of `radix` from the front of `chars`.
fn digits(chars: &mut Chars<'_>, radix: u32, max: usize) -> String {
    let mut out = String::new();
    while out.len() < max && chars.peek().is_some_and(|c| c.is_digit(radix)) {
        out.extend(chars.next());
    }
    out
}

/// One escape sequence after a backslash, as bytes: `\a \b \f \n \r \t \v \s`, `\xHH` and three octal
/// digits (one byte each), `\uXXXX` and `\UXXXXXXXX` (one character, UTF-8), a quote or backslash as
/// itself. An unknown or incomplete escape stays as written.
fn unescape(chars: &mut Chars<'_>) -> Vec<u8> {
    let kept = |text: String| text.into_bytes();
    let unicode = |hex: &str, want: usize| {
        let ch = (hex.len() == want)
            .then(|| u32::from_str_radix(hex, 16).ok().and_then(char::from_u32))
            .flatten();
        ch.map(|c| c.to_string().into_bytes())
    };
    match chars.next() {
        None => b"\\".to_vec(),
        Some('a') => vec![0x07],
        Some('b') => vec![0x08],
        Some('f') => vec![0x0c],
        Some('n') => vec![b'\n'],
        Some('r') => vec![b'\r'],
        Some('t') => vec![b'\t'],
        Some('v') => vec![0x0b],
        Some('s') => vec![b' '],
        Some(c @ ('\\' | '"' | '\'')) => kept(c.to_string()),
        Some('x') => {
            let hex = digits(chars, 16, 2);
            match u8::from_str_radix(&hex, 16) {
                Ok(byte) if hex.len() == 2 => vec![byte],
                _ => kept(format!("\\x{hex}")),
            }
        }
        Some(c @ '0'..='3') => {
            let oct = format!("{c}{}", digits(chars, 8, 2));
            match u8::from_str_radix(&oct, 8) {
                Ok(byte) if oct.len() == 3 => vec![byte],
                _ => kept(format!("\\{oct}")),
            }
        }
        Some('u') => {
            let hex = digits(chars, 16, 4);
            unicode(&hex, 4).unwrap_or_else(|| kept(format!("\\u{hex}")))
        }
        Some('U') => {
            let hex = digits(chars, 16, 8);
            unicode(&hex, 8).unwrap_or_else(|| kept(format!("\\U{hex}")))
        }
        Some(c) => kept(format!("\\{c}")),
    }
}

/// One program a unit runs: the unit's coverage key, the program's file name and its path as written
/// (`%h` written `~`).
#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub struct Consumer {
    pub unit: String,
    pub bin: String,
    pub path: String,
}

/// The programs the tracked unit files run, from every `Exec*` setting whose program is an absolute
/// path or under `%h`: any directory counts, so a binary run from `/opt/...` is seen like one run from
/// `/usr/local/bin`. Reads the files only.
pub fn consumers(repo: &Path, unit_files: &BTreeSet<String>) -> Result<BTreeSet<Consumer>, String> {
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
            let program = first_word(value).replacen("%h", "~", 1);
            if !(program.starts_with('/') || program.starts_with("~/")) {
                continue;
            }
            if let Some(bin) = program.rsplit('/').next().filter(|b| !b.is_empty()) {
                out.insert(Consumer {
                    unit: unit.clone(),
                    bin: bin.to_owned(),
                    path: program.clone(),
                });
            }
        }
    }
    Ok(out)
}

/// The env files the tracked unit files read: `(unit key, file name)` of every `EnvironmentFile=` line
/// the file name relative to `%h/.config/plangraph/` or `/etc/plangraph/` (any other path as written, quotes and
/// spaces included: systemd does not unquote it), with the leading `-` (optional file) dropped and `%i` kept.
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
            // systemd reads the rest of the line as the path, spaces included and quotes not special.
            let value = value.trim_start_matches('-');
            if value.is_empty() {
                continue; // an empty setting only clears the list
            }
            // Outside the two plangraph directories the whole path stays: the manifest holds file names
            // under those directories only, so such a file is reported, never skipped.
            let name = ["%h/.config/plangraph/", "/etc/plangraph/"]
                .iter()
                .find_map(|dir| value.strip_prefix(dir))
                .unwrap_or(value);
            out.insert((unit.clone(), name.to_owned()));
        }
    }
    Ok(out)
}

/// `(package, bin)` of every binary target of every workspace member under `rust/`, as Cargo lists
/// them (`cargo metadata`): explicit `[[bin]]`, `src/main.rs`, `src/bin/*`, member globs, `exclude`
/// and `autobins = false` are Cargo's own business, not a second implementation here.
pub fn bin_targets(repo: &Path) -> Result<BTreeSet<(String, String)>, String> {
    let rust = repo.join("rust");
    let cargo = std::env::var_os("CARGO").unwrap_or_else(|| "cargo".into());
    let mut command = Command::new(cargo);
    command.current_dir(&rust).args([
        "metadata",
        "--format-version",
        "1",
        "--no-deps",
        "--offline",
    ]);
    if rust.join("Cargo.lock").is_file() {
        command.arg("--locked");
    }
    let output = command
        .output()
        .map_err(|e| format!("cargo metadata: {e}"))?;
    if !output.status.success() {
        return Err(format!(
            "cargo metadata in {}: {}",
            rust.display(),
            String::from_utf8_lossy(&output.stderr).trim()
        ));
    }
    let metadata: serde_json::Value =
        serde_json::from_slice(&output.stdout).map_err(|e| format!("cargo metadata: {e}"))?;
    let mut out = BTreeSet::new();
    for package in metadata["packages"].as_array().into_iter().flatten() {
        let name = package["name"].as_str().ok_or("package without a name")?;
        for target in package["targets"].as_array().into_iter().flatten() {
            let is_bin = target["kind"]
                .as_array()
                .is_some_and(|kinds| kinds.iter().any(|k| k == "bin"));
            if let (true, Some(bin)) = (is_bin, target["name"].as_str()) {
                out.insert((name.to_owned(), bin.to_owned()));
            }
        }
    }
    Ok(out)
}

/// `true` when the tracked unit file `deploy/<key>` is a template an installer must render: a `*.in`
/// file, or a unit with an `@NAME@` placeholder (`@ROOT@`, `@PYTHON@`, `@ROUTER_DIR@`).
pub fn needs_rendering(repo: &Path, key: &str) -> Result<bool, String> {
    if key.ends_with(".in") {
        return Ok(true);
    }
    let path = repo.join("deploy").join(key);
    let text = fs::read_to_string(&path).map_err(|e| format!("{}: {e}", path.display()))?;
    let placeholder = regex::Regex::new(r"@[A-Z][A-Z0-9_]*@").map_err(|e| e.to_string())?;
    Ok(placeholder.is_match(&text))
}
