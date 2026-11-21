//! Token-free readiness; scratch Git metadata never points at the real checkout.
use crate::host_command;
use std::{
    fs, io,
    path::{Path, PathBuf},
    process::Command,
    time::Duration,
};

pub const WRITER_SANDBOX: &str = "workspace-write";
const MAX_AGE_SECS: u64 = 120;

pub fn valid_sandbox(sandbox: &str) -> bool {
    matches!(sandbox, WRITER_SANDBOX | "read-only" | "danger-full-access")
}

/// Common and per-worktree Git directories that need write access for commits.
pub fn writer_git_dirs(cwd: &Path, path: Option<&str>) -> io::Result<Vec<PathBuf>> {
    let mut command = Command::new("git");
    if let Some(path) = path {
        command.env("PATH", path);
    }
    let out = host_command::output(
        command.current_dir(cwd).args([
            "rev-parse",
            "--path-format=absolute",
            "--git-common-dir",
            "--absolute-git-dir",
        ]),
        Duration::from_secs(15),
    )
    .map_err(|e| io::Error::other(format!("git directory lookup failed: {e}")))?;
    let text = std::str::from_utf8(&out).map_err(io::Error::other)?;
    let dirs: Vec<_> = text.lines().map(PathBuf::from).collect();
    if dirs.len() != 2 || dirs.iter().any(|p| !p.is_absolute()) {
        return Err(io::Error::other(
            "git directory lookup did not return two absolute paths",
        ));
    }
    Ok(dirs)
}

/// The workspace-write overrides used by the native writer and its readiness probe.
pub fn writer_sandbox_args(roots: &[PathBuf]) -> io::Result<Vec<String>> {
    let list = roots
        .iter()
        .map(|root| {
            let text = root
                .to_str()
                .ok_or_else(|| io::Error::other("writable root is not UTF-8"))?;
            serde_json::to_string(text).map_err(io::Error::other)
        })
        .collect::<io::Result<Vec<_>>>()?
        .join(",");
    Ok([
        format!("sandbox_mode=\"{WRITER_SANDBOX}\""),
        "sandbox_workspace_write.network_access=true".to_owned(),
        format!("sandbox_workspace_write.writable_roots=[{list}]"),
    ]
    .into_iter()
    .flat_map(|value| ["-c".to_owned(), value])
    .collect())
}

fn record_path(state: &Path, host: &str, sandbox: &str) -> io::Result<PathBuf> {
    if !crate::fleet_series::valid_host(host) || !valid_sandbox(sandbox) {
        return Err(io::Error::other("invalid readiness host or sandbox"));
    }
    Ok(state
        .join("package-readiness")
        .join(host)
        .join(format!("{sandbox}.json")))
}

#[derive(Clone, Debug, serde::Deserialize, serde::Serialize)]
pub struct Record {
    pub host: String,
    pub sandbox: String,
    pub checked_at: u64,
    pub errors: Vec<String>,
}

impl Record {
    pub fn read(state: &Path, host: &str, sandbox: &str) -> io::Result<Self> {
        Ok(serde_json::from_slice(&fs::read(record_path(
            state, host, sandbox,
        )?)?)?)
    }

    pub fn save(&self, state: &Path) -> io::Result<()> {
        crate::run::record_io::atomic_write_private(
            &record_path(state, &self.host, &self.sandbox)?,
            &serde_json::to_vec(self)?,
        )
    }

    pub fn refusal(&self, host: &str, sandbox: &str, now: u64) -> Option<&'static str> {
        if self.host != host {
            Some("readiness host mismatch")
        } else if self.sandbox != sandbox {
            Some("readiness sandbox mismatch")
        } else if self.checked_at > now {
            Some("future readiness")
        } else if now - self.checked_at > MAX_AGE_SECS {
            Some("stale readiness")
        } else if !self.errors.is_empty() {
            Some("readiness check failed")
        } else {
            None
        }
    }
}

pub struct Config {
    pub sandbox: String,
    pub project_trusted: bool,
    pub hooks_trusted: bool,
    pub update_prompt_disabled: bool,
    pub mem_available_mb: Option<u64>,
    pub min_mem_mb: u64,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Probe {
    Version,
    Login,
    Sandbox,
    Commit,
}
pub trait Runner {
    fn probe(&mut self, probe: Probe, scratch: &Path, sandbox: &str) -> io::Result<()>;
}

pub fn check(config: &Config, scratch: &Path, runner: &mut impl Runner) -> Vec<String> {
    let mut errors = Vec::new();
    for (probe, reason) in [
        (Probe::Version, "codex version unavailable"),
        (Probe::Login, "codex login unavailable"),
        (Probe::Sandbox, "sandbox cannot start"),
        (Probe::Commit, "scratch worktree commit failed"),
    ] {
        if runner.probe(probe, scratch, &config.sandbox).is_err() {
            errors.push(reason.into());
        }
    }
    for (ok, reason) in [
        (config.project_trusted, "folder trust missing"),
        (
            config.hooks_trusted,
            "hook trust unverified: review configured hooks",
        ),
        (
            config.update_prompt_disabled,
            "startup update prompt not disabled",
        ),
        (
            config
                .mem_available_mb
                .is_some_and(|m| m >= config.min_mem_mb),
            "free memory below floor or unavailable",
        ),
    ] {
        if !ok {
            errors.push(reason.into());
        }
    }
    errors
}

pub fn settings(codex_home: &Path, project: &Path, sandbox: String) -> io::Result<Config> {
    let text = fs::read_to_string(codex_home.join("config.toml"))?;
    let config: toml::Value = text.parse().map_err(io::Error::other)?;
    let project = project.canonicalize()?;
    let trusted = config
        .get("projects")
        .and_then(toml::Value::as_table)
        .is_some_and(|projects| {
            projects.iter().any(|(path, value)| {
                Path::new(path).canonicalize().is_ok_and(|p| p == project)
                    && value.get("trust_level").and_then(toml::Value::as_str) == Some("trusted")
            })
        });
    let local_path = project.join(".codex/config.toml");
    let local = match fs::read_to_string(&local_path) {
        Ok(text) => Some(text.parse::<toml::Value>().map_err(io::Error::other)?),
        Err(e) if e.kind() == io::ErrorKind::NotFound => None,
        Err(e) => return Err(e),
    };
    // Trust hashes are CLI-owned. Fail closed for hooks instead of guessing its storage schema.
    let hooks_absent = config.get("hooks").is_none()
        && local.as_ref().is_none_or(|c| c.get("hooks").is_none())
        && !codex_home.join("hooks.json").exists()
        && !project.join(".codex/hooks.json").exists()
        && config.get("plugins").is_none();
    Ok(Config {
        sandbox,
        project_trusted: trusted,
        hooks_trusted: hooks_absent,
        update_prompt_disabled: config
            .get("check_for_update_on_startup")
            .and_then(toml::Value::as_bool)
            == Some(false),
        mem_available_mb: crate::host_probe::probe("readiness")
            .ok()
            .map(|p| p.mem_available_mb),
        min_mem_mb: 3000,
    })
}

pub struct Native {
    pub codex: PathBuf,
    pub executable: PathBuf,
}
fn git(dir: &Path, args: &[&std::ffi::OsStr]) -> io::Result<()> {
    host_command::output(
        Command::new("git")
            .current_dir(dir)
            .args([
                "-c",
                "user.name=Readiness",
                "-c",
                "user.email=readiness@example.invalid",
                "-c",
                "commit.gpgsign=false",
            ])
            .args(args),
        Duration::from_secs(15),
    )
    .map(|_| ())
}
pub fn commit_child(worktree: &Path) -> io::Result<()> {
    fs::write(worktree.join("ready.txt"), "ready\n")?;
    git(worktree, &["add".as_ref(), "ready.txt".as_ref()])?;
    git(
        worktree,
        &[
            "commit".as_ref(),
            "-qm".as_ref(),
            "readiness probe".as_ref(),
        ],
    )
}
impl Runner for Native {
    fn probe(&mut self, probe: Probe, scratch: &Path, sandbox: &str) -> io::Result<()> {
        let mut cmd = Command::new(&self.codex);
        match probe {
            Probe::Version => {
                cmd.arg("--version");
            }
            Probe::Login => {
                cmd.args(["login", "status"]);
            }
            Probe::Sandbox | Probe::Commit => {
                let repo = scratch.join("repository");
                let worktree = scratch.join("worktree");
                if !repo.exists() {
                    fs::create_dir(&repo)?;
                    git(
                        &repo,
                        &["init".as_ref(), "-q".as_ref(), "--template=".as_ref()],
                    )?;
                    git(
                        &repo,
                        &[
                            "commit".as_ref(),
                            "--allow-empty".as_ref(),
                            "-qm".as_ref(),
                            "initial".as_ref(),
                        ],
                    )?;
                    git(
                        &repo,
                        &[
                            "worktree".as_ref(),
                            "add".as_ref(),
                            "--detach".as_ref(),
                            worktree.as_os_str(),
                        ],
                    )?;
                }
                cmd.current_dir(&worktree).arg("sandbox");
                if sandbox == WRITER_SANDBOX {
                    cmd.args(writer_sandbox_args(&writer_git_dirs(&worktree, None)?)?);
                } else {
                    cmd.args(["-c", &format!("sandbox_mode=\"{sandbox}\"")]);
                }
                cmd.arg("--")
                    .arg(&self.executable)
                    .arg(if probe == Probe::Sandbox {
                        "--child-start"
                    } else {
                        "--child-commit"
                    });
            }
        }
        host_command::output(&mut cmd, Duration::from_secs(30)).map(|_| ())
    }
}
