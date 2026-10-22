//! Agent harnesses: how a coding agent is invoked inside a worktree, plus the
//! sandbox pre-check that must hold before it runs.
//!
//! A harness only *builds* a command; `run_harness` hands that command to
//! `stall::supervise_with_cancel`, which owns the process group, output capture
//! and the stall/timeout/cancel kills. Keeping the two apart lets tests inspect
//! argv and cwd without spawning anything.
//!
//! The sandbox check exists because a symlink *inside* the worktree can point
//! outside it: a write through the link lands beyond the write set even though
//! the path it was written to looks confined. Such links are resolved purely
//! lexically, so a broken link is judged the same as a working one.

use pg_worker_core::scope::{self, Jail, ScopeLimits, system_ro};
use pg_worker_core::stall::{Opts, Supervised, supervise_with_cancel};
use std::collections::HashMap;
use std::path::{Component, Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::Arc;

/// One agent invocation: where it runs, which model, what to do.
pub struct HarnessRun<'a> {
    pub workdir: &'a Path,
    pub model: &'a str,
    pub prompt: &'a str,
}

/// Builds the agent's command. The caller runs it with `stall::supervise_with_cancel`.
pub trait Harness: Send + Sync {
    fn name(&self) -> &str;
    fn command(&self, run: &HarnessRun) -> Command;
    /// The executable behind this harness, for the startup version pin.
    /// `None` for a harness with no single binary (a test double).
    fn binary(&self) -> Option<&str> {
        None
    }
    /// Refuse a run whose arguments could be parsed as options by the CLI
    /// (option smuggling). The prompt is safe because it follows `--`; a
    /// model name has no such protection.
    fn validate(&self, run: &HarnessRun) -> Result<(), String> {
        reject_option_like("model", run.model)
    }
}

/// An argument the CLI would read as an option (leading `-`) or an empty one.
fn reject_option_like(what: &str, value: &str) -> Result<(), String> {
    if value.is_empty() || value.starts_with('-') {
        return Err(format!(
            "refusing {what} {value:?}: empty or starts with '-'"
        ));
    }
    Ok(())
}

/// The `opencode run` CLI.
#[derive(Debug, Clone)]
pub struct Opencode {
    pub bin: String,
}

impl Opencode {
    pub fn new() -> Self {
        Opencode {
            bin: "opencode".into(),
        }
    }
}

impl Default for Opencode {
    fn default() -> Self {
        Self::new()
    }
}

impl Harness for Opencode {
    fn name(&self) -> &str {
        "opencode"
    }

    fn binary(&self) -> Option<&str> {
        Some(&self.bin)
    }

    fn command(&self, run: &HarnessRun) -> Command {
        let mut cmd = Command::new(&self.bin);
        // No shell: the prompt is a single argv element, so nothing in it is parsed.
        cmd.arg("run")
            // Tools run in this process tree, not the shared host service (which
            // would sit outside the attempt jail and the worker's confined env).
            .arg("--standalone")
            .arg("-m")
            .arg(run.model)
            // `--` ends options: a prompt starting with `-` stays the prompt.
            .arg("--")
            .arg(run.prompt)
            .current_dir(run.workdir)
            .stdin(Stdio::null())
            // A self-update mid-run would replace the very binary we supervise.
            .env("OPENCODE_DISABLE_AUTOUPDATE", "1");
        cmd
    }
}

/// The Claude Code CLI in print mode: `claude -p <prompt> --model <m>`.
///
/// Same shape as [`Opencode`]: no shell, the prompt is one argv element, stdin
/// is closed and the process runs in the attempt worktree, so the worktree
/// sandbox and the write-set confinement apply unchanged. Claude is one leg
/// among several, never a hard dependency of the worker.
#[derive(Debug, Clone)]
pub struct ClaudeCode {
    pub bin: String,
}

impl ClaudeCode {
    pub fn new() -> Self {
        ClaudeCode {
            bin: "claude".into(),
        }
    }
}

impl Default for ClaudeCode {
    fn default() -> Self {
        Self::new()
    }
}

impl Harness for ClaudeCode {
    fn name(&self) -> &str {
        "claude"
    }

    fn binary(&self) -> Option<&str> {
        Some(&self.bin)
    }

    fn command(&self, run: &HarnessRun) -> Command {
        let mut cmd = Command::new(&self.bin);
        cmd.arg("-p")
            .arg("--model")
            .arg(run.model)
            // `--` ends options: a prompt starting with `-` stays the prompt.
            .arg("--")
            .arg(run.prompt)
            .current_dir(run.workdir)
            .stdin(Stdio::null())
            // A self-update mid-run would replace the very binary we supervise.
            .env("DISABLE_AUTOUPDATER", "1");
        cmd
    }
}

/// The env var naming the opencode server an [`OpencodeServer`] leg attaches to.
/// It must be a bare `http(s)://host[:port]` URL: userinfo (`user:pass@`) is
/// rejected because the URL lands in the child's argv (visible to every local
/// user via the process table). Tokens and passwords go through the staged
/// harness auth files (`PG_HARNESS_AUTH_DIR`), never through the URL, argv or
/// the child's environment.
pub const OPENCODE_SERVER_ENV: &str = "PG_OPENCODE_SERVER_URL";

/// Why an [`OpencodeServer`] could not be built.
#[derive(Debug, thiserror::Error)]
pub enum HarnessConfigError {
    #[error("{0} is not set; there is deliberately no default server")]
    MissingServerUrl(&'static str),
    #[error("{0} must be an http:// or https:// URL")]
    BadServerScheme(&'static str),
    #[error(
        "{0} must not carry credentials (user:pass@); stage tokens via the harness auth dir instead"
    )]
    ServerUrlUserinfo(&'static str),
}

/// `opencode run --attach <url> -m <model> <prompt>`: the opencode CLI driving
/// an already running opencode server. This is the fallback leg when a hosted
/// provider is rate limited. The URL comes only from configuration, never from
/// a default, so no host is baked into the worker.
#[derive(Debug, Clone)]
pub struct OpencodeServer {
    pub bin: String,
    pub url: String,
    pub allow_attach: bool,
}

impl OpencodeServer {
    pub fn new(url: impl Into<String>) -> Self {
        OpencodeServer {
            bin: "opencode".into(),
            url: url.into(),
            allow_attach: false,
        }
    }

    /// Allow the attach leg for an attended run: its tools run outside the jail.
    pub fn with_attach_allowed(mut self) -> Self {
        self.allow_attach = true;
        self
    }

    /// Build from `PG_OPENCODE_SERVER_URL`; an unset or empty value is an error.
    /// The attach leg is allowed only when `PG_ALLOW_ATTACH=1`.
    pub fn from_env() -> Result<Self, HarnessConfigError> {
        let allow = Self::attach_allowed(std::env::var("PG_ALLOW_ATTACH").ok().as_deref());
        Self::from_value_with(std::env::var(OPENCODE_SERVER_ENV).ok().as_deref(), allow)
    }

    /// The env lookup without the env, so it is testable without mutating it.
    #[cfg(test)]
    fn from_value(value: Option<&str>) -> Result<Self, HarnessConfigError> {
        Self::from_value_with(value, false)
    }

    fn attach_allowed(value: Option<&str>) -> bool {
        value == Some("1")
    }

    fn from_value_with(
        value: Option<&str>,
        allow_attach: bool,
    ) -> Result<Self, HarnessConfigError> {
        match value.map(str::trim) {
            Some(url) if !url.is_empty() => {
                let lower = url.to_ascii_lowercase();
                let rest = lower
                    .strip_prefix("http://")
                    .or_else(|| lower.strip_prefix("https://"))
                    .ok_or(HarnessConfigError::BadServerScheme(OPENCODE_SERVER_ENV))?;
                let authority = rest.split(['/', '?', '#']).next().unwrap_or("");
                if authority.contains('@') {
                    return Err(HarnessConfigError::ServerUrlUserinfo(OPENCODE_SERVER_ENV));
                }
                let mut server = Self::new(url);
                server.allow_attach = allow_attach;
                Ok(server)
            }
            _ => Err(HarnessConfigError::MissingServerUrl(OPENCODE_SERVER_ENV)),
        }
    }
}

impl Harness for OpencodeServer {
    fn name(&self) -> &str {
        "opencode-server"
    }

    fn binary(&self) -> Option<&str> {
        Some(&self.bin)
    }

    fn command(&self, run: &HarnessRun) -> Command {
        let mut cmd = Command::new(&self.bin);
        cmd.arg("run")
            .arg("--attach")
            .arg(&self.url)
            .arg("-m")
            .arg(run.model)
            .arg("--")
            .arg(run.prompt)
            .current_dir(run.workdir)
            .stdin(Stdio::null())
            .env("OPENCODE_DISABLE_AUTOUPDATE", "1");
        cmd
    }

    fn validate(&self, run: &HarnessRun) -> Result<(), String> {
        if !self.allow_attach {
            return Err(
                "attach leg refused for unattended cards: its tools run outside \
                        the attempt jail (set PG_ALLOW_ATTACH=1 for an attended run)"
                    .into(),
            );
        }
        reject_option_like("model", run.model)?;
        reject_option_like("server url", &self.url)
    }
}

/// Harnesses by name. A leg's `harness` field selects one; a name nobody
/// registered is a contract defect, not a crash.
#[derive(Clone, Default)]
pub struct HarnessRegistry {
    by_name: HashMap<String, Arc<dyn Harness>>,
}

impl HarnessRegistry {
    pub fn new() -> Self {
        Self::default()
    }

    /// Register `h` under `name`, replacing any earlier entry.
    pub fn register(&mut self, name: impl Into<String>, h: Arc<dyn Harness>) -> &mut Self {
        self.by_name.insert(name.into(), h);
        self
    }

    /// Builder form of [`register`](Self::register).
    pub fn with(mut self, name: impl Into<String>, h: Arc<dyn Harness>) -> Self {
        self.register(name, h);
        self
    }

    pub fn get(&self, name: &str) -> Option<Arc<dyn Harness>> {
        self.by_name.get(name).cloned()
    }

    /// Registered names, sorted.
    pub fn names(&self) -> Vec<String> {
        let mut v: Vec<String> = self.by_name.keys().cloned().collect();
        v.sort();
        v
    }
}

/// Build the command for `h` and run it under the stall/timeout watchdog.
///
/// The child never inherits the worker's environment: it gets the exact-name
/// allowlist of [`pg_worker_core::sandbox_env`] (`PATH`, `LANG`, `TERM`,
/// toolchain locations), whatever the harness set on its command itself, and
/// `home` as `HOME`. `PG_*`, `TEMPORAL_*`, provider keys and the agent CLIs'
/// own credentials therefore stay with the worker: harness auth is staged
/// into `home` as files (`sandbox_env::HomeGuard::stage_harness_auth`),
/// never passed by env.
///
/// `cancelled` is polled while the child runs so a cancel kills the process
/// group promptly instead of waiting for stall, timeout or natural exit.
pub fn run_harness(
    h: &dyn Harness,
    run: &HarnessRun,
    home: &Path,
    opts: &Opts,
    cancelled: &dyn Fn() -> bool,
    jail: Option<&JailConfig>,
) -> std::io::Result<Supervised> {
    h.validate(run)
        .map_err(|e| std::io::Error::new(std::io::ErrorKind::InvalidInput, e))?;
    let mut cmd = h.command(run);
    pg_worker_core::sandbox_env::confine_harness(&mut cmd, home);
    if let Some(j) = jail {
        cmd.env("TMPDIR", &j.tmp);
        cmd = scope::wrap(
            cmd,
            &j.limits,
            &Jail {
                bin: &j.bin,
                rw: vec![
                    run.workdir.to_path_buf(),
                    home.to_path_buf(),
                    j.tmp.clone(),
                    PathBuf::from("/dev/null"),
                ],
                ro: system_ro(),
            },
        );
    }
    supervise_with_cancel(cmd, opts, cancelled)
}

/// Env var naming the Landlock jail helper binary.
pub const JAIL_BIN_ENV: &str = "PG_JAIL_BIN";
/// `PG_JAIL=off` is the explicit dev override for running attempts unjailed.
pub const JAIL_OFF_ENV: &str = "PG_JAIL";

/// Filesystem jail and resource limits for one attempt.
#[derive(Debug, Clone)]
pub struct JailConfig {
    pub bin: PathBuf,
    pub limits: ScopeLimits,
    pub tmp: PathBuf,
}

impl JailConfig {
    /// None when `PG_JAIL_BIN` is unset (library default: tests, tools).
    pub fn from_env() -> Result<Option<JailConfig>, String> {
        Self::from_lookup(|k| std::env::var(k).ok())
    }

    /// Startup policy of the worker binary: jailed or explicitly disabled.
    pub fn required_from_env() -> Result<Option<JailConfig>, String> {
        Self::required_from_lookup(|k| std::env::var(k).ok())
    }

    fn from_lookup<F>(lookup: F) -> Result<Option<JailConfig>, String>
    where
        F: Fn(&str) -> Option<String>,
    {
        let Some(bin) = lookup(JAIL_BIN_ENV) else {
            return Ok(None);
        };
        let bin = bin.trim();
        if bin.is_empty() {
            return Err(format!(
                "{JAIL_BIN_ENV} is empty; expected an absolute file path"
            ));
        }
        let path = Path::new(bin);
        if !path.is_absolute() {
            return Err(format!("{JAIL_BIN_ENV} {bin:?} must be an absolute path"));
        }
        let bin = std::fs::canonicalize(path)
            .map_err(|e| format!("{JAIL_BIN_ENV} {bin:?}: cannot canonicalize: {e}"))?;
        if !bin.is_file() {
            return Err(format!("{JAIL_BIN_ENV} {bin:?} is not a file"));
        }
        Ok(Some(JailConfig {
            bin,
            limits: Self::limits_from_lookup(&lookup)?,
            tmp: std::env::temp_dir(),
        }))
    }

    fn required_from_lookup<F>(lookup: F) -> Result<Option<JailConfig>, String>
    where
        F: Fn(&str) -> Option<String>,
    {
        if lookup(JAIL_BIN_ENV).is_some() {
            let cfg = Self::from_lookup(&lookup)?.ok_or_else(|| {
                format!("{JAIL_BIN_ENV} is empty; refusing to run attempts unjailed")
            })?;
            return Ok(Some(cfg));
        }
        if lookup(JAIL_OFF_ENV).as_deref() == Some("off") {
            return Ok(None);
        }
        Err(format!(
            "{JAIL_BIN_ENV} is not set; refusing to run attempts unjailed \
             ({JAIL_OFF_ENV}=off is the explicit dev override)"
        ))
    }

    fn limits_from_lookup<F>(lookup: &F) -> Result<ScopeLimits, String>
    where
        F: Fn(&str) -> Option<String>,
    {
        let mut limits = ScopeLimits::default();
        if let Some(v) = lookup("PG_ATTEMPT_MEMORY_MAX") {
            let v = v.trim();
            if !v.is_empty() {
                limits.memory_max = v.to_string();
            }
        }
        if let Some(v) = lookup("PG_ATTEMPT_TASKS_MAX") {
            let v = v.trim();
            limits.tasks_max = v
                .parse::<u32>()
                .map_err(|e| format!("PG_ATTEMPT_TASKS_MAX {v:?}: {e}"))?;
        }
        Ok(limits)
    }
}

/// Env var pinning harness versions, `name=version` pairs separated by commas
/// (for example `opencode=1.2.3,claude=2.0.1`). Unset means no pin.
pub const VERSIONS_ENV: &str = "PG_HARNESS_VERSIONS";

/// Why the startup version pin refused to start the worker.
#[derive(Debug, thiserror::Error, PartialEq, Eq)]
pub enum PinError {
    #[error("{VERSIONS_ENV} entry {0:?} is not name=version")]
    Malformed(String),
    #[error("harness {0:?} is pinned but not registered or has no binary")]
    Unavailable(String),
    #[error("harness {name:?} could not report its version: {why}")]
    Unrunnable { name: String, why: String },
    #[error("harness {name:?} is version {found:?}, pinned to {want:?}")]
    Mismatch {
        name: String,
        want: String,
        found: String,
    },
}

/// Parse `name=version,name=version`. Empty entries are skipped.
pub fn parse_pins(spec: &str) -> Result<Vec<(String, String)>, PinError> {
    let mut out = Vec::new();
    for part in spec.split(',').map(str::trim).filter(|p| !p.is_empty()) {
        match part.split_once('=') {
            Some((n, v)) if !n.trim().is_empty() && !v.trim().is_empty() => {
                out.push((n.trim().to_string(), v.trim().to_string()));
            }
            _ => return Err(PinError::Malformed(part.to_string())),
        }
    }
    Ok(out)
}

/// True when the `--version` output names exactly `want` as one of its
/// whitespace-separated tokens (a leading `v` is ignored), so `1.2.3` does not
/// match `1.2.30`.
fn version_matches(output: &str, want: &str) -> bool {
    let want = want.trim_start_matches('v');
    output
        .split_whitespace()
        .any(|t| t.trim_start_matches('v') == want)
}

/// A throwaway private `HOME` for a `--version` probe: a freshly created
/// directory with a random name and mode 0700 (never a predictable path in the
/// shared temp dir), removed on drop.
struct ProbeHome(tempfile::TempDir);

impl ProbeHome {
    fn new(name: &str) -> std::io::Result<ProbeHome> {
        let safe: String = name
            .chars()
            .filter(|c| c.is_ascii_alphanumeric())
            .take(16)
            .collect();
        tempfile::Builder::new()
            .prefix(&format!("pg-probe-{safe}-"))
            .suffix(".home")
            .tempdir()
            .map(ProbeHome)
    }

    fn path(&self) -> &Path {
        self.0.path()
    }
}

/// Fail closed on a version drift: for every pinned name run `<bin> --version`
/// and require the pinned version. A pinned harness that is not registered, has
/// no binary, cannot run or reports another version is an error. Harnesses
/// without a pin are not checked.
pub fn verify_pins(reg: &HarnessRegistry, pins: &[(String, String)]) -> Result<(), PinError> {
    for (name, want) in pins {
        let bin = reg
            .get(name)
            .and_then(|h| h.binary().map(str::to_string))
            .ok_or_else(|| PinError::Unavailable(name.clone()))?;
        // Same confined env and a private HOME as a real run: the probe must
        // not see the worker's PG_* / TEMPORAL_* either.
        let home = ProbeHome::new(name).map_err(|e| PinError::Unrunnable {
            name: name.clone(),
            why: format!("private HOME: {e}"),
        })?;
        let opts = Opts {
            timeout: Some(std::time::Duration::from_secs(15)),
            grace: std::time::Duration::from_secs(1),
            ..Opts::default()
        };
        // `supervise` retries a spawn that hits ETXTBSY (see `stall`).
        let mut cmd = Command::new(&bin);
        cmd.arg("--version").stdin(Stdio::null());
        pg_worker_core::sandbox_env::confine_harness(&mut cmd, home.path());
        let r = pg_worker_core::stall::supervise(cmd, &opts).map_err(|e| PinError::Unrunnable {
            name: name.clone(),
            why: e.to_string(),
        })?;
        let out = String::from_utf8_lossy(&r.output).into_owned();
        if r.code != Some(0) || r.timed_out {
            return Err(PinError::Unrunnable {
                name: name.clone(),
                why: format!("exit {:?}, timed out: {}", r.code, r.timed_out),
            });
        }
        if !version_matches(&out, want) {
            return Err(PinError::Mismatch {
                name: name.clone(),
                want: want.clone(),
                found: out.trim().to_string(),
            });
        }
    }
    Ok(())
}

/// Apply [`VERSIONS_ENV`] (already read by the caller) to `reg`. `None` or an
/// empty value is "no pin": the caller logs a warning and goes on.
pub fn check_version_pins(reg: &HarnessRegistry, spec: Option<&str>) -> Result<bool, PinError> {
    match spec.map(str::trim) {
        Some(s) if !s.is_empty() => {
            verify_pins(reg, &parse_pins(s)?)?;
            Ok(true)
        }
        _ => Ok(false),
    }
}

/// Errors from the symlink sandbox check.
#[derive(Debug, thiserror::Error)]
pub enum SandboxError {
    #[error("worktree has symlinks escaping it: {0:?}")]
    Escape(Vec<PathBuf>),
    #[error(transparent)]
    Io(#[from] std::io::Error),
}

/// Reject the worktree if any symlink in it resolves outside it.
pub fn check_sandbox(workdir: &Path) -> Result<(), SandboxError> {
    let escapes = find_escaping_symlinks(workdir)?;
    if escapes.is_empty() {
        Ok(())
    } else {
        Err(SandboxError::Escape(escapes))
    }
}

/// Every symlink under `workdir` (relative to it, sorted) whose target
/// resolves outside `workdir`.
///
/// Targets are resolved lexically, without touching the filesystem, so a broken
/// link cannot hide by pointing at a path that does not exist yet.
pub fn find_escaping_symlinks(workdir: &Path) -> std::io::Result<Vec<PathBuf>> {
    let base = lexical(&std::path::absolute(workdir)?);
    let mut out = Vec::new();
    walk(workdir, workdir, &base, true, &mut out)?;
    out.sort();
    Ok(out)
}

fn walk(
    root: &Path,
    dir: &Path,
    base: &Path,
    top: bool,
    out: &mut Vec<PathBuf>,
) -> std::io::Result<()> {
    for entry in std::fs::read_dir(dir)? {
        let entry = entry?;
        let path = entry.path();
        // file_type comes from symlink_metadata: it never follows the link.
        let ft = entry.file_type()?;
        let top_git = top && entry.file_name().to_str() == Some(".git");
        // The worktree's own top-level `.git` is repo metadata, never part of
        // the write set: git makes it a directory (a checkout) or a plain
        // gitfile (a linked worktree), so skipping it leaves every normal
        // commit untouched. Git never makes `.git` a symlink, though — one at
        // the top level was planted to redirect git at some other repository,
        // and that is a violation wherever it points. A `.git` deeper down
        // never had the exemption.
        if top_git && !ft.is_symlink() {
            continue;
        }
        if ft.is_symlink() {
            let rel = path
                .strip_prefix(root)
                .map(Path::to_path_buf)
                .unwrap_or_else(|_| path.clone());
            if top_git {
                out.push(rel);
                continue;
            }
            let parent = base.join(rel.parent().unwrap_or(Path::new("")));
            let target = std::fs::read_link(&path)?;
            let resolved = if target.is_absolute() {
                lexical(&target)
            } else {
                lexical(&parent.join(&target))
            };
            if !resolved.starts_with(base) {
                out.push(rel);
            }
        } else if ft.is_dir() {
            walk(root, &path, base, false, out)?;
        }
    }
    Ok(())
}

/// Normalize `.` and `..` textually, with no filesystem access.
fn lexical(path: &Path) -> PathBuf {
    let mut out = PathBuf::new();
    for c in path.components() {
        match c {
            Component::Prefix(_) | Component::RootDir => out.push(c.as_os_str()),
            Component::CurDir => {}
            Component::ParentDir => {
                // Pop a real component; `..` above a root simply stays put.
                if !out.pop() && out.as_os_str().is_empty() {
                    out.push("..");
                }
            }
            Component::Normal(s) => out.push(s),
        }
    }
    out
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;
    use std::ffi::OsStr;
    use std::os::unix::fs::symlink;

    fn link(workdir: &Path, at: &str, target: impl AsRef<Path>) -> PathBuf {
        let p = workdir.join(at);
        if let Some(parent) = p.parent() {
            std::fs::create_dir_all(parent).unwrap();
        }
        symlink(target, &p).unwrap();
        p
    }

    #[test]
    fn opencode_command_has_args_cwd_and_env() {
        let h = Opencode::new();
        let run = HarnessRun {
            workdir: Path::new("/tmp/w"),
            model: "some/model",
            prompt: "fix the bug",
        };
        let c = h.command(&run);
        assert_eq!(c.get_program(), OsStr::new("opencode"));
        let args: Vec<String> = c
            .get_args()
            .map(|a| a.to_string_lossy().into_owned())
            .collect();
        assert_eq!(
            args,
            [
                "run",
                "--standalone",
                "-m",
                "some/model",
                "--",
                "fix the bug"
            ]
        );
        assert_eq!(c.get_current_dir(), Some(Path::new("/tmp/w")));
        assert_eq!(
            c.get_envs()
                .find(|(k, _)| k.to_str() == Some("OPENCODE_DISABLE_AUTOUPDATE"))
                .and_then(|(_, v)| v),
            Some(OsStr::new("1"))
        );
    }

    fn argv(c: &Command) -> Vec<String> {
        c.get_args()
            .map(|a| a.to_string_lossy().into_owned())
            .collect()
    }

    #[test]
    fn claude_command_is_print_mode_without_a_shell() {
        let run = HarnessRun {
            workdir: Path::new("/tmp/w"),
            model: "some-claude-model",
            prompt: "fix; rm -rf $HOME",
        };
        let c = ClaudeCode::new().command(&run);
        assert_eq!(c.get_program(), OsStr::new("claude"));
        // The hostile-looking prompt stays one argv element.
        assert_eq!(
            argv(&c),
            [
                "-p",
                "--model",
                "some-claude-model",
                "--",
                "fix; rm -rf $HOME"
            ]
        );
        assert_eq!(c.get_current_dir(), Some(Path::new("/tmp/w")));
    }

    #[test]
    fn opencode_server_attaches_to_the_configured_url() {
        let run = HarnessRun {
            workdir: Path::new("/tmp/w"),
            model: "some/model",
            prompt: "fix the bug",
        };
        let h = OpencodeServer::new("http://127.0.0.1:4096");
        assert_eq!(h.name(), "opencode-server");
        let c = h.command(&run);
        assert_eq!(c.get_program(), OsStr::new("opencode"));
        assert_eq!(
            argv(&c),
            [
                "run",
                "--attach",
                "http://127.0.0.1:4096",
                "-m",
                "some/model",
                "--",
                "fix the bug"
            ]
        );
        assert_eq!(c.get_current_dir(), Some(Path::new("/tmp/w")));
    }

    #[test]
    fn opencode_server_has_no_default_url() {
        assert!(matches!(
            OpencodeServer::from_value(None),
            Err(HarnessConfigError::MissingServerUrl(_))
        ));
        assert!(OpencodeServer::from_value(Some("  ")).is_err());
        for bad in [
            "http://user:pw@127.0.0.1:4096",
            "https://tok@127.0.0.1:4096/x",
            "HTTP://u:p@127.0.0.1",
            "ftp://127.0.0.1:4096",
            "file:///etc/passwd",
            "127.0.0.1:4096",
            "--evil",
        ] {
            assert!(OpencodeServer::from_value(Some(bad)).is_err(), "{bad}");
        }
        assert!(matches!(
            OpencodeServer::from_value(Some("http://u:p@127.0.0.1")),
            Err(HarnessConfigError::ServerUrlUserinfo(_))
        ));
        // An `@` after the authority (path/query) is not userinfo.
        assert!(OpencodeServer::from_value(Some("https://127.0.0.1:4096/a@b")).is_ok());
        let h = OpencodeServer::from_value(Some(" http://127.0.0.1:4096 ")).unwrap();
        assert_eq!(h.url, "http://127.0.0.1:4096");
    }

    #[test]
    fn option_like_prompt_stays_after_double_dash_in_every_harness() {
        let run = HarnessRun {
            workdir: Path::new("/tmp/w"),
            model: "m",
            prompt: "--dangerously-skip-permissions",
        };
        let hs: Vec<Box<dyn Harness>> = vec![
            Box::new(Opencode::new()),
            Box::new(ClaudeCode::new()),
            Box::new(OpencodeServer::new("http://127.0.0.1:4096")),
        ];
        for h in hs {
            let a = argv(&h.command(&run));
            let n = a.len();
            assert_eq!(a[n - 2], "--", "{}: {a:?}", h.name());
            assert_eq!(a[n - 1], run.prompt);
        }
    }

    #[test]
    fn option_like_model_or_url_is_refused() {
        let bad = HarnessRun {
            workdir: Path::new("/tmp/w"),
            model: "--config=evil",
            prompt: "p",
        };
        let ok = HarnessRun {
            model: "some/model",
            ..HarnessRun {
                workdir: bad.workdir,
                model: "",
                prompt: "p",
            }
        };
        for h in [
            &Opencode::new() as &dyn Harness,
            &ClaudeCode::new(),
            &OpencodeServer::new("http://127.0.0.1:4096").with_attach_allowed(),
        ] {
            assert!(h.validate(&bad).is_err(), "{}", h.name());
            assert!(h.validate(&ok).is_ok(), "{}", h.name());
        }
        assert!(
            OpencodeServer::new("--evil")
                .with_attach_allowed()
                .validate(&ok)
                .is_err()
        );
        let home = tempfile::tempdir().unwrap();
        let e = run_harness(
            &Opencode::new(),
            &bad,
            home.path(),
            &Opts::default(),
            &|| false,
            None,
        )
        .unwrap_err();
        assert_eq!(e.kind(), std::io::ErrorKind::InvalidInput);
    }

    #[test]
    fn registry_looks_up_by_name() {
        let reg = HarnessRegistry::new()
            .with("opencode", Arc::new(Opencode::new()))
            .with("claude", Arc::new(ClaudeCode::new()));
        assert_eq!(reg.names(), ["claude", "opencode"]);
        assert_eq!(reg.get("claude").unwrap().name(), "claude");
        assert!(reg.get("opencode-server").is_none());
    }

    struct Fake {
        script: &'static str,
    }

    impl Harness for Fake {
        fn name(&self) -> &str {
            "fake"
        }
        fn command(&self, _run: &HarnessRun) -> Command {
            let mut c = Command::new("sh");
            c.arg("-c").arg(self.script);
            c
        }
    }

    #[test]
    fn run_harness_supervises_and_captures_output() {
        let dir = tempfile::tempdir().unwrap();
        let run = HarnessRun {
            workdir: dir.path(),
            model: "m",
            prompt: "p",
        };
        let home = tempfile::tempdir().unwrap();
        let r = run_harness(
            &Fake { script: "echo hi" },
            &run,
            home.path(),
            &Opts::default(),
            &|| false,
            None,
        )
        .unwrap();
        assert_eq!(r.code, Some(0));
        assert!(String::from_utf8_lossy(&r.output).contains("hi"));
    }

    #[test]
    fn harness_child_does_not_see_worker_secrets() {
        let dir = tempfile::tempdir().unwrap();
        let home = tempfile::tempdir().unwrap();
        let run = HarnessRun {
            workdir: dir.path(),
            model: "m",
            prompt: "p",
        };
        // Unique to this test: no other test reads or sets it.
        // SAFETY: nothing else touches this name, and the child is spawned below.
        unsafe { std::env::set_var("PG_SENTINEL_SECRET_HARNESS", "hunter2") };
        let r = run_harness(
            &Fake { script: "env" },
            &run,
            home.path(),
            &Opts::default(),
            &|| false,
            None,
        )
        .unwrap();
        let out = String::from_utf8_lossy(&r.output).into_owned();
        assert!(
            !out.contains("SENTINEL") && !out.contains("hunter2"),
            "{out}"
        );
        assert!(
            out.contains(&format!("HOME={}", home.path().display())),
            "{out}"
        );
        assert!(out.contains("PATH="), "{out}");
    }

    #[test]
    fn harness_own_env_survives_the_scrub() {
        let dir = tempfile::tempdir().unwrap();
        let home = tempfile::tempdir().unwrap();
        let run = HarnessRun {
            workdir: dir.path(),
            model: "m",
            prompt: "p",
        };
        // The real harnesses set this on their own command.
        struct Own;
        impl Harness for Own {
            fn name(&self) -> &str {
                "own"
            }
            fn command(&self, _r: &HarnessRun) -> Command {
                let mut c = Command::new("sh");
                c.arg("-c")
                    .arg("echo auto=$OPENCODE_DISABLE_AUTOUPDATE")
                    .env("OPENCODE_DISABLE_AUTOUPDATE", "1");
                c
            }
        }
        let r = run_harness(&Own, &run, home.path(), &Opts::default(), &|| false, None).unwrap();
        assert!(String::from_utf8_lossy(&r.output).contains("auto=1"));
    }

    #[test]
    fn real_harnesses_report_their_binary() {
        assert_eq!(Opencode::new().binary(), Some("opencode"));
        assert_eq!(ClaudeCode::new().binary(), Some("claude"));
        assert_eq!(OpencodeServer::new("u").binary(), Some("opencode"));
        assert_eq!(Fake { script: "" }.binary(), None);
    }

    #[test]
    fn pins_parse_and_reject_garbage() {
        assert_eq!(
            parse_pins("opencode=1.2.3, claude=2.0.1,").unwrap(),
            vec![
                ("opencode".to_string(), "1.2.3".to_string()),
                ("claude".to_string(), "2.0.1".to_string())
            ]
        );
        assert!(parse_pins("").unwrap().is_empty());
        for bad in ["opencode", "=1", "opencode="] {
            assert_eq!(
                parse_pins(bad).unwrap_err(),
                PinError::Malformed(bad.to_string())
            );
        }
    }

    #[test]
    fn version_match_is_exact_per_token() {
        assert!(version_matches("opencode 1.2.3\n", "1.2.3"));
        assert!(version_matches("v1.2.3", "1.2.3"));
        assert!(!version_matches("1.2.30", "1.2.3"));
        assert!(!version_matches("", "1.2.3"));
    }

    /// A fake CLI that answers `--version` with `out` and the given exit code.
    fn fake_bin(dir: &Path, name: &str, out: &str, code: i32) -> String {
        crate::testutil::write_script(
            dir,
            name,
            &format!("#!/bin/sh\necho '{out}'\nexit {code}\n"),
        )
        .to_string_lossy()
        .into_owned()
    }

    fn registry_with(bin: String) -> HarnessRegistry {
        HarnessRegistry::new().with("opencode", Arc::new(Opencode { bin }))
    }

    #[test]
    fn version_pin_accepts_a_match_and_rejects_a_drift() {
        let d = tempfile::tempdir().unwrap();
        let reg = registry_with(fake_bin(d.path(), "oc", "opencode 1.2.3", 0));
        assert_eq!(check_version_pins(&reg, Some("opencode=1.2.3")), Ok(true));
        assert_eq!(
            check_version_pins(&reg, Some("opencode=9.9.9")),
            Err(PinError::Mismatch {
                name: "opencode".into(),
                want: "9.9.9".into(),
                found: "opencode 1.2.3".into(),
            })
        );
    }

    #[test]
    fn version_probe_child_sees_no_worker_secrets() {
        let d = tempfile::tempdir().unwrap();
        // The fake CLI prints its environment; the pin matches on a token that
        // is present only if PG_/TEMPORAL_ variables are absent.
        let p = crate::testutil::write_script(
            d.path(),
            "envdump",
            "#!/bin/sh\nif env | grep -q '^PG_PROBE_SENTINEL=\\|^TEMPORAL_PROBE_SENTINEL='; then echo leaked; else echo clean-1.0; fi\n",
        );
        // Unique names; no other test reads or sets them.
        unsafe { std::env::set_var("PG_PROBE_SENTINEL", "x") };
        unsafe { std::env::set_var("TEMPORAL_PROBE_SENTINEL", "x") };
        let reg = registry_with(p.to_string_lossy().into_owned());
        assert_eq!(
            check_version_pins(&reg, Some("opencode=clean-1.0")),
            Ok(true)
        );
    }

    #[test]
    fn version_pin_fails_closed_when_unavailable() {
        let d = tempfile::tempdir().unwrap();
        // Pinned but not registered.
        let reg = registry_with(fake_bin(d.path(), "oc", "opencode 1.2.3", 0));
        assert_eq!(
            check_version_pins(&reg, Some("claude=2.0.1")),
            Err(PinError::Unavailable("claude".into()))
        );
        // Registered, but the binary does not exist.
        let missing = registry_with(d.path().join("nope").to_string_lossy().into_owned());
        assert!(matches!(
            check_version_pins(&missing, Some("opencode=1.2.3")),
            Err(PinError::Unrunnable { .. })
        ));
        // Registered, but `--version` fails.
        let broken = registry_with(fake_bin(d.path(), "bad", "boom", 3));
        assert!(matches!(
            check_version_pins(&broken, Some("opencode=1.2.3")),
            Err(PinError::Unrunnable { .. })
        ));
        // Garbage in the variable is an error, not a silent no-pin.
        assert!(matches!(
            check_version_pins(&reg, Some("opencode")),
            Err(PinError::Malformed(_))
        ));
    }

    #[test]
    fn unset_pin_is_no_pin() {
        let reg = HarnessRegistry::new();
        assert_eq!(check_version_pins(&reg, None), Ok(false));
        assert_eq!(check_version_pins(&reg, Some("  ")), Ok(false));
    }

    #[test]
    fn symlink_inside_the_worktree_is_ok() {
        let dir = tempfile::tempdir().unwrap();
        let w = dir.path();
        std::fs::write(w.join("target.txt"), "x").unwrap();
        link(w, "alias", "target.txt");
        assert!(find_escaping_symlinks(w).unwrap().is_empty());
        assert!(check_sandbox(w).is_ok());
    }

    #[test]
    fn symlink_out_of_the_worktree_escapes() {
        let dir = tempfile::tempdir().unwrap();
        let w = dir.path();
        link(w, "passwd", "/etc/passwd");
        assert_eq!(
            find_escaping_symlinks(w).unwrap(),
            vec![PathBuf::from("passwd")]
        );
        assert!(matches!(check_sandbox(w), Err(SandboxError::Escape(_))));
    }

    #[test]
    fn relative_link_climbing_out_escapes() {
        let dir = tempfile::tempdir().unwrap();
        let w = dir.path();
        link(w, "up", "../../x");
        assert_eq!(
            find_escaping_symlinks(w).unwrap(),
            vec![PathBuf::from("up")]
        );
    }

    #[test]
    fn dotdot_that_returns_inside_is_ok() {
        let dir = tempfile::tempdir().unwrap();
        let w = dir.path();
        link(w, "l", "sub/../ok");
        assert!(find_escaping_symlinks(w).unwrap().is_empty());
    }

    #[test]
    fn absolute_link_inside_is_ok() {
        let dir = tempfile::tempdir().unwrap();
        let w = dir.path();
        std::fs::write(w.join("ok"), "x").unwrap();
        symlink(w.join("ok"), w.join("abs")).unwrap();
        assert!(find_escaping_symlinks(w).unwrap().is_empty());
    }

    #[test]
    fn git_dir_is_skipped() {
        let dir = tempfile::tempdir().unwrap();
        let w = dir.path();
        link(w, ".git/evil", "/etc/passwd");
        assert!(find_escaping_symlinks(w).unwrap().is_empty());
        assert!(check_sandbox(w).is_ok());
    }

    #[test]
    fn nested_git_symlink_escapes_but_top_level_git_is_skipped() {
        let dir = tempfile::tempdir().unwrap();
        let w = dir.path();
        // Top-level `.git` is the repo's own metadata and stays exempt...
        link(w, ".git/evil", "/etc/passwd");
        // ...but a `.git` nested below it must not borrow that exemption.
        link(w, "sub/.git", "/etc");
        assert_eq!(
            find_escaping_symlinks(w).unwrap(),
            vec![PathBuf::from("sub/.git")]
        );
        assert!(matches!(check_sandbox(w), Err(SandboxError::Escape(_))));
    }

    #[test]
    fn top_level_dot_git_symlink_is_a_violation_wherever_it_points() {
        let dir = tempfile::tempdir().unwrap();
        let w = dir.path();
        // The target sits *inside* the worktree: a top-level `.git` link is
        // refused because git must never follow one, not because it escapes.
        std::fs::write(w.join("elsewhere"), "gitdir: /somewhere").unwrap();
        link(w, ".git", "elsewhere");
        assert_eq!(
            find_escaping_symlinks(w).unwrap(),
            vec![PathBuf::from(".git")]
        );
        assert!(matches!(check_sandbox(w), Err(SandboxError::Escape(_))));
    }

    #[test]
    fn top_level_dot_git_gitfile_stays_exempt() {
        let dir = tempfile::tempdir().unwrap();
        let w = dir.path();
        // What `git worktree add` writes is a plain file, never a link, and
        // every normal commit goes through it.
        std::fs::write(w.join(".git"), "gitdir: ../main/.git/worktrees/w\n").unwrap();
        assert!(find_escaping_symlinks(w).unwrap().is_empty());
        assert!(check_sandbox(w).is_ok());
    }

    fn attach_run() -> HarnessRun<'static> {
        HarnessRun {
            workdir: Path::new("/tmp/w"),
            model: "some/model",
            prompt: "p",
        }
    }

    #[test]
    fn attach_leg_is_refused_by_default() {
        let e = OpencodeServer::new("http://127.0.0.1:4096")
            .validate(&attach_run())
            .unwrap_err();
        assert!(e.contains("attach leg refused"), "{e}");
    }

    #[test]
    fn attach_leg_can_be_allowed() {
        assert!(
            OpencodeServer::new("http://127.0.0.1:4096")
                .with_attach_allowed()
                .validate(&attach_run())
                .is_ok()
        );
    }

    #[test]
    fn attach_allow_follows_the_env_value() {
        let url = Some("http://127.0.0.1:4096");
        let refused =
            OpencodeServer::from_value_with(url, OpencodeServer::attach_allowed(None)).unwrap();
        assert!(refused.validate(&attach_run()).is_err());
        let allowed =
            OpencodeServer::from_value_with(url, OpencodeServer::attach_allowed(Some("1")))
                .unwrap();
        assert!(allowed.validate(&attach_run()).is_ok());
        assert!(!OpencodeServer::attach_allowed(Some("0")));
    }

    #[test]
    fn jail_is_off_when_the_binary_is_unset() {
        assert!(JailConfig::from_lookup(|_| None).unwrap().is_none());
    }

    #[test]
    fn required_jail_refuses_unset_without_the_override() {
        let e = JailConfig::required_from_lookup(|_| None).unwrap_err();
        assert!(e.contains("PG_JAIL_BIN is not set"), "{e}");
    }

    #[test]
    fn required_jail_allows_explicit_off() {
        let r =
            JailConfig::required_from_lookup(|k| (k == JAIL_OFF_ENV).then(|| "off".to_string()));
        assert!(r.unwrap().is_none());
    }

    #[test]
    fn jail_rejects_a_bad_tasks_value() {
        let bin = tempfile::NamedTempFile::new().unwrap();
        let e = JailConfig::from_lookup(|k| match k {
            JAIL_BIN_ENV => Some(bin.path().to_string_lossy().into_owned()),
            "PG_ATTEMPT_TASKS_MAX" => Some("many".to_string()),
            _ => None,
        })
        .unwrap_err();
        assert!(e.contains("PG_ATTEMPT_TASKS_MAX"), "{e}");
    }

    #[test]
    fn jail_binary_must_be_an_existing_absolute_file() {
        let dir = tempfile::tempdir().unwrap();
        for path in [
            PathBuf::from("pg-jail"),
            dir.path().join("missing"),
            dir.path().to_path_buf(),
        ] {
            for required in [false, true] {
                let lookup =
                    |k: &str| (k == JAIL_BIN_ENV).then(|| path.to_string_lossy().into_owned());
                let result = if required {
                    JailConfig::required_from_lookup(lookup)
                } else {
                    JailConfig::from_lookup(lookup)
                };
                assert!(result.unwrap_err().contains(JAIL_BIN_ENV));
            }
        }
    }

    #[test]
    fn jail_binary_is_stored_canonicalized() {
        let bin = tempfile::NamedTempFile::new().unwrap();
        let alias = bin.path().with_extension("link");
        symlink(bin.path(), &alias).unwrap();
        let cfg = JailConfig::from_lookup(|k| {
            (k == JAIL_BIN_ENV).then(|| alias.to_string_lossy().into_owned())
        })
        .unwrap()
        .unwrap();
        assert_eq!(cfg.bin, std::fs::canonicalize(bin.path()).unwrap());
        std::fs::remove_file(alias).unwrap();
    }
}
