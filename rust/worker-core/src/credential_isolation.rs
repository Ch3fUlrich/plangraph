//! Credential isolation of a sandboxed agent run: one deny list of secret paths, the Codex
//! permission profile that applies it, and the refusal of a card that asks for more than the
//! writer sandbox.
//!
//! `workspace-write` alone lets a run read the whole filesystem, so the keeper DSN files and
//! tokens in the plangraph config directory, the ssh directory and the cloud credentials are
//! readable by every command the agent runs. The profile built here keeps the same writable
//! roots and network access and adds a `none` entry per secret path.
use serde::Deserialize;
use serde_json::Value;
use std::{
    io,
    path::{Path, PathBuf},
};

/// Name of the permission profile passed to Codex.
pub const PROFILE: &str = "plangraph_isolated";
/// The sandbox a spawned run gets; the only value a card may name.
pub const SANDBOX: &str = "workspace-write";
/// Prefix of the refusal reason of a card that asks for a wider sandbox.
pub const REFUSED: &str = "sandbox_refused:";

const SECRET_PATHS: &str = include_str!("../../../src/plangraph/config/secret_paths.json");

#[derive(Deserialize)]
struct SecretPaths {
    /// Relative to the home directory.
    home_relative: Vec<String>,
    /// Relative to the config home (`XDG_CONFIG_HOME`, else `~/.config`).
    config_home_relative: Vec<String>,
    /// Extra home-relative files only the session print guard denies: the Codex sandbox leaves
    /// them readable because nested Codex runs authenticate with them.
    session_guard_extra_home_relative: Vec<String>,
    /// Variable name patterns Codex removes from the environment of the commands it runs.
    tool_env_exclude: Vec<String>,
}

fn data() -> SecretPaths {
    serde_json::from_str(SECRET_PATHS).expect("secret_paths.json is valid and tested")
}

/// Secret paths a sandboxed run must not read: the data file's home entries, and the config
/// entries under both the default `~/.config` and `xdg_config_home` when that differs.
pub fn deny_read_paths(home: &Path, xdg_config_home: Option<&Path>) -> Vec<PathBuf> {
    let data: SecretPaths =
        serde_json::from_str(SECRET_PATHS).expect("secret_paths.json is valid and tested");
    let mut config_homes = vec![home.join(".config")];
    config_homes.extend(
        xdg_config_home
            .filter(|dir| dir.is_absolute())
            .map(Path::to_path_buf),
    );
    let mut paths: Vec<PathBuf> = Vec::new();
    let all =
        data.home_relative
            .iter()
            .map(|rel| home.join(rel))
            .chain(config_homes.iter().flat_map(|base| {
                data.config_home_relative
                    .iter()
                    .map(move |rel| base.join(rel))
            }));
    for path in all {
        if !paths.contains(&path) {
            paths.push(path);
        }
    }
    paths
}

/// The paths a fleet session may not print: the sandbox deny list plus the Codex auth file.
pub fn session_guard_paths(home: &Path, xdg_config_home: Option<&Path>) -> Vec<PathBuf> {
    let mut paths = deny_read_paths(home, xdg_config_home);
    for rel in &data().session_guard_extra_home_relative {
        let path = home.join(rel);
        if !paths.contains(&path) {
            paths.push(path);
        }
    }
    paths
}

/// The plangraph config directories of a host: under the default `~/.config` and under an
/// absolute `xdg_config_home`.
pub fn plangraph_config_dirs(home: &Path, xdg_config_home: Option<&Path>) -> Vec<PathBuf> {
    let mut bases = vec![home.join(".config")];
    bases.extend(
        xdg_config_home
            .filter(|dir| dir.is_absolute())
            .map(Path::to_path_buf),
    );
    let data = data();
    bases
        .iter()
        .flat_map(|base| {
            data.config_home_relative
                .iter()
                .map(move |rel| base.join(rel))
        })
        .collect()
}

/// `HOME` (else `USERPROFILE`) and `XDG_CONFIG_HOME` of this process.
fn host_dirs() -> io::Result<(PathBuf, Option<PathBuf>)> {
    let home = std::env::var_os("HOME")
        .or_else(|| std::env::var_os("USERPROFILE"))
        .map(PathBuf::from)
        .filter(|home| home.is_absolute())
        .ok_or_else(|| io::Error::other("credential isolation needs an absolute HOME"))?;
    Ok((home, std::env::var_os("XDG_CONFIG_HOME").map(PathBuf::from)))
}

/// The plangraph config directories of this process's host.
pub fn host_plangraph_config_dirs() -> io::Result<Vec<PathBuf>> {
    let (home, xdg) = host_dirs()?;
    Ok(plangraph_config_dirs(&home, xdg.as_deref()))
}

/// The deny list of this process: `HOME` and `XDG_CONFIG_HOME` from the environment. A missing
/// `HOME` is an error, never an empty list.
pub fn host_deny_read_paths() -> io::Result<Vec<PathBuf>> {
    let (home, xdg) = host_dirs()?;
    Ok(deny_read_paths(&home, xdg.as_deref()))
}

fn toml_string(path: &Path) -> io::Result<String> {
    let text = path
        .to_str()
        .ok_or_else(|| io::Error::other("sandbox path is not UTF-8"))?;
    serde_json::to_string(text).map_err(io::Error::other)
}

/// Codex `-c` arguments of the isolated profile: read everywhere except `deny`, write to the
/// project root, the temporary directories and `write_roots`, network access on. The profile
/// is a filesystem map where the more specific path wins, so a denied path inside a writable
/// root stays denied.
pub fn profile_args(write_roots: &[PathBuf], deny: &[PathBuf]) -> io::Result<Vec<String>> {
    let mut entries = vec![
        "\":root\"=\"read\"".to_owned(),
        "\":project_roots\"={\".\"=\"write\"}".to_owned(),
        "\":tmpdir\"=\"write\"".to_owned(),
        "\"/tmp\"=\"write\"".to_owned(),
    ];
    for root in write_roots {
        entries.push(format!("{}=\"write\"", toml_string(root)?));
    }
    for path in deny {
        entries.push(format!("{}=\"none\"", toml_string(path)?));
    }
    Ok([
        format!("default_permissions=\"{PROFILE}\""),
        format!("permissions.{PROFILE}.filesystem={{{}}}", entries.join(",")),
        format!("permissions.{PROFILE}.network.enabled=true"),
    ]
    .into_iter()
    .flat_map(|value| ["-c".to_owned(), value])
    .collect())
}

/// Codex `-c` arguments that keep the listed variables (harness API keys) out of the commands
/// the agent runs, on top of Codex's default removal of names with KEY, SECRET or TOKEN.
pub fn env_policy_args() -> Vec<String> {
    let list = data()
        .tool_env_exclude
        .iter()
        .map(|pattern| serde_json::to_string(pattern).expect("a string serializes"))
        .collect::<Vec<_>>()
        .join(",");
    [
        "shell_environment_policy.ignore_default_excludes=false".to_owned(),
        format!("shell_environment_policy.exclude=[{list}]"),
    ]
    .into_iter()
    .flat_map(|value| ["-c".to_owned(), value])
    .collect()
}

/// Refuse a card that names a sandbox other than the writer sandbox. `danger-full-access`
/// has its own reason; an absent or null `sandbox` is the default and passes.
pub fn refuse_card_sandbox(card: &Value) -> Result<(), String> {
    match card.get("sandbox").filter(|value| !value.is_null()) {
        None => Ok(()),
        Some(Value::String(mode)) if mode == SANDBOX => Ok(()),
        Some(Value::String(mode)) if mode == "danger-full-access" => Err(format!(
            "{REFUSED} danger-full-access is never granted to a spawned run; \
             the run gets {SANDBOX} with the secret paths denied"
        )),
        Some(other) => Err(format!(
            "{REFUSED} card.sandbox {other} is not supported; only {SANDBOX} is granted"
        )),
    }
}

/// What the probe child reports for `path`, from an open that reads no bytes: `denied` when the
/// sandbox refuses the open, `readable` when it succeeds, `absent` or `error` otherwise.
pub fn child_open(path: &Path) -> &'static str {
    match std::fs::File::open(path) {
        Ok(_) => "readable",
        Err(e) if e.kind() == io::ErrorKind::PermissionDenied => "denied",
        Err(e) if e.kind() == io::ErrorKind::NotFound => "absent",
        Err(_) => "error",
    }
}

/// Probe the effective writer sandbox policy (`writer_sandbox_args`) with the real Codex
/// sandbox: every existing path of `deny` must open as `denied` from inside it, `exe` itself as
/// the `readable` control, and at least one existing path of `required` (the plangraph config
/// directories) must have been probed, all of them `denied`. Returns one `<verdict> <path>` line
/// per probe and whether all verdicts were as required. No file is read. `scratch` becomes the
/// sandbox's writable project root: it must not contain a missing denied path, because the
/// sandbox would create mount points for those inside it.
pub fn probe(
    codex: &Path,
    exe: &Path,
    scratch: &Path,
    deny: &[PathBuf],
    required: &[PathBuf],
) -> io::Result<(Vec<String>, bool)> {
    let verdict = |path: &Path| sandboxed_open(codex, exe, scratch, deny, path, None);
    let mut lines = vec![format!("{} {}", verdict(exe)?, "control")];
    let mut ok = lines[0].starts_with("readable");
    let mut config_denied = 0;
    for path in deny.iter().filter(|p| std::fs::symlink_metadata(p).is_ok()) {
        let word = verdict(path)?;
        ok &= word == "denied";
        if required.contains(path) {
            config_denied += usize::from(word == "denied");
        }
        lines.push(format!("{word} {}", path.display()));
    }
    if config_denied == 0 {
        lines.push("absent no plangraph config directory was probed as denied".to_owned());
    }
    Ok((lines, ok && config_denied > 0))
}

/// What `exe --child-open path` prints inside the writer sandbox that denies `deny`.
fn sandboxed_open(
    codex: &Path,
    exe: &Path,
    scratch: &Path,
    deny: &[PathBuf],
    path: &Path,
    tool_path: Option<&str>,
) -> io::Result<String> {
    let mut command = std::process::Command::new(codex);
    if let Some(tool_path) = tool_path {
        command.env("PATH", tool_path);
    }
    command
        .arg("sandbox")
        .args(crate::host_readiness::writer_sandbox_args(&[], deny)?)
        .arg("--")
        .arg(exe)
        .arg("--child-open")
        .arg(path)
        .current_dir(scratch);
    let out = crate::host_command::capture(&mut command, std::time::Duration::from_secs(30))?;
    let text = String::from_utf8_lossy(&out.stdout);
    Ok(match text.trim() {
        word @ ("denied" | "readable" | "absent" | "error") => word.to_owned(),
        _ => "unavailable".to_owned(),
    })
}

/// Readiness check of the effective writer policy: a fixture file placed under `scratch` and
/// denied together with the host's secret paths must open as `denied`. Errs with the reason when
/// the sandbox starts but does not enforce the denial.
pub fn check_fixture_denied(
    codex: &Path,
    exe: &Path,
    scratch: &Path,
    tool_path: Option<&str>,
) -> io::Result<()> {
    let fixture = scratch.join("credential-fixture");
    std::fs::create_dir_all(&fixture)?;
    let secret = fixture.join("fixture-secret");
    std::fs::write(&secret, "fixture")?;
    let mut deny = host_deny_read_paths()?;
    deny.push(fixture);
    match sandboxed_open(codex, exe, scratch, &deny, &secret, tool_path)?.as_str() {
        "denied" => Ok(()),
        other => Err(io::Error::other(format!(
            "credential isolation is not enforced: a denied fixture file opened as {other}"
        ))),
    }
}

#[cfg(test)]
#[path = "credential_isolation_tests.rs"]
mod tests;
