//! The host side of a train deploy (`land-train deploy-host --op check|install|read|disable --unit U
//! --repo CHECKOUT --rev LANDED`), run on the host that installs. It works from the host's checkout, which
//! must contain the landed revision before anything is installed.
//!
//! A unit is one the deploy manifest names; its family's files are copied into the user unit directory when
//! they differ, the enabled unit is enabled, and the unit is restarted (a oneshot runs once); `install`
//! prints the invocation the restart started. `bin:<name>` is built and published by
//! `host-sync --install-release` and read back with `host-sync --check-release`, so `installed` means the
//! published binary was built from a tree that contains the landed revision. `check` is the install dry
//! run: one `MISSING binary|file|env|path NAME` line per absent thing.
use super::deploy_derive::Crates;
use super::deploy_run::{RunRead, ABSENT};
use crate::deploy_manifest::{Manifest, UnitEntry};
use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

/// The commands a host step runs, so tests see them without a host.
pub trait Sys {
    /// Runs `argv`; returns whether it exited 0 and its stdout.
    fn run(&self, argv: &[&str]) -> Result<(bool, String), String>;
    fn home(&self) -> PathBuf;
}

pub struct RealSys;

impl Sys for RealSys {
    fn run(&self, argv: &[&str]) -> Result<(bool, String), String> {
        let (cmd, args) = argv.split_first().ok_or("empty command")?;
        let out = std::process::Command::new(cmd)
            .args(args)
            .output()
            .map_err(|e| format!("{cmd}: {e}"))?;
        Ok((
            out.status.success(),
            String::from_utf8_lossy(&out.stdout).into_owned(),
        ))
    }
    fn home(&self) -> PathBuf {
        std::env::var_os("HOME")
            .map(PathBuf::from)
            .unwrap_or_default()
    }
}

/// One host step.
pub struct Step<'a> {
    pub sys: &'a dyn Sys,
    pub repo: PathBuf,
    pub rev: String,
}

const SHOW: &str = "LoadState,ActiveState,Result,ExecMainExitTimestamp,InvocationID,Type";

/// A [`RunRead`] from `systemctl show -p ...` output; a unit systemd does not know is `absent`.
pub fn parse_show(text: &str) -> RunRead {
    let props: BTreeMap<&str, &str> = text.lines().filter_map(|l| l.split_once('=')).collect();
    let get = |k: &str| {
        props
            .get(k)
            .map(|v| v.trim())
            .filter(|v| !v.is_empty() && *v != "n/a")
    };
    let absent = get("LoadState") == Some("not-found");
    RunRead {
        is_active: if absent {
            ABSENT.into()
        } else {
            get("ActiveState").unwrap_or("unknown").into()
        },
        result: get("Result").map(str::to_owned),
        exec_main_exit_timestamp: get("ExecMainExitTimestamp").map(str::to_owned),
        oneshot: get("Type") == Some("oneshot"),
        invocation: get("InvocationID").map(str::to_owned),
    }
}

/// The first `ExecStart=` program of a unit file, with `%h` as the home directory; `None` when it has a
/// placeholder or is not absolute.
fn exec_program(unit_text: &str, home: &Path) -> Option<PathBuf> {
    let line = unit_text
        .lines()
        .find_map(|l| l.trim().strip_prefix("ExecStart="))?;
    let program = line
        .trim_start_matches(['-', '@', ':', '+', '!'])
        .split_whitespace()
        .next()?;
    let program = program.replace("%h", &home.to_string_lossy());
    (program.starts_with('/') && !program.contains('@')).then(|| PathBuf::from(program))
}

impl Step<'_> {
    fn manifest(&self) -> Result<Manifest, String> {
        Manifest::load_from_repo(&self.repo)
    }

    /// The manifest family that installs `unit`.
    fn entry(&self, manifest: &Manifest, unit: &str) -> Result<UnitEntry, String> {
        manifest
            .unit
            .iter()
            .find(|u| {
                u.installed_enable_unit().ok() == Some(unit)
                    || u.installed_service_unit().as_deref() == Some(unit)
            })
            .cloned()
            .ok_or_else(|| {
                format!(
                    "{unit} is no enabled unit or timer service of {}",
                    crate::deploy_manifest::MANIFEST_PATH
                )
            })
    }

    fn git(&self, args: &[&str]) -> Result<(bool, String), String> {
        let repo = self.repo.to_string_lossy();
        let mut argv = vec!["git", "-C", &repo];
        argv.extend(args);
        self.sys.run(&argv)
    }

    /// The checkout head, which must contain the landed revision.
    fn require_landed(&self) -> Result<String, String> {
        let (_, head) = self.git(&["rev-parse", "HEAD"])?;
        let head = head.trim().to_owned();
        match self.git(&["merge-base", "--is-ancestor", &self.rev, &head])? {
            (true, _) => Ok(head),
            _ => Err(format!("the checkout at {head} does not contain the landed revision {}; sync the host first", self.rev)),
        }
    }

    /// `--with-keeper-backup` when `bin` is an optional host binary of its crate.
    fn release_flag(&self, bin: &str) -> Vec<&'static str> {
        let tomls: BTreeMap<String, String> = std::fs::read_dir(self.repo.join("rust"))
            .into_iter()
            .flatten()
            .flatten()
            .filter_map(|d| {
                let text = std::fs::read_to_string(d.path().join("Cargo.toml")).ok()?;
                Some((d.file_name().to_string_lossy().into_owned(), text))
            })
            .collect();
        let optional = Crates::marks(&tomls, "optional-host-binaries");
        if optional.contains(bin) {
            vec!["--with-keeper-backup"]
        } else {
            vec![]
        }
    }

    pub fn check(&self, unit: &str) -> Result<Vec<String>, String> {
        let mut missing = Vec::new();
        if unit.starts_with("bin:") {
            let path = std::env::var_os("PATH");
            if crate::host_sync::resolve_on_path("host-sync", path.as_deref()).is_none() {
                missing.push("MISSING binary host-sync".to_owned());
            }
            return Ok(missing);
        }
        let manifest = self.manifest()?;
        let entry = self.entry(&manifest, unit)?;
        let home = self.sys.home();
        for file in &entry.files {
            let path = self.repo.join(entry.source_dir()).join(file);
            match std::fs::read_to_string(&path) {
                Ok(text) => {
                    if let Some(program) = exec_program(&text, &home).filter(|p| !p.exists()) {
                        missing.push(format!("MISSING binary {}", program.display()));
                    }
                }
                Err(_) => missing.push(format!("MISSING file {}/{file}", entry.source_dir())),
            }
        }
        let config = home.join(".config/plangraph");
        for env in &entry.env_files {
            if !env
                .split('|')
                .any(|name| config.join(name.replace("%i", "")).exists())
            {
                missing.push(format!("MISSING env {env}"));
            }
        }
        Ok(missing)
    }

    /// Installs `unit` and returns the invocation its restart started.
    pub fn install(&self, unit: &str) -> Result<Option<String>, String> {
        self.require_landed()?;
        let repo = self.repo.to_string_lossy().into_owned();
        if let Some(bin) = unit.strip_prefix("bin:") {
            let mut argv = vec!["host-sync", "--install-release", &repo];
            argv.extend(self.release_flag(bin));
            return match self.sys.run(&argv)? {
                (true, _) => Ok(None),
                _ => Err(format!("host-sync --install-release failed for {bin}")),
            };
        }
        let entry = self.entry(&self.manifest()?, unit)?;
        let dir = self.sys.home().join(".config/systemd/user");
        std::fs::create_dir_all(&dir).map_err(|e| format!("{}: {e}", dir.display()))?;
        for file in entry.files.iter().filter(|f| !f.ends_with(".in")) {
            let source = self.repo.join(entry.source_dir()).join(file);
            let text = std::fs::read(&source).map_err(|e| format!("{}: {e}", source.display()))?;
            let target = dir.join(file);
            if std::fs::read(&target).ok().as_deref() != Some(text.as_slice()) {
                std::fs::write(&target, &text).map_err(|e| format!("{}: {e}", target.display()))?;
            }
        }
        let enable = entry.installed_enable_unit()?.to_owned();
        for argv in [
            vec!["systemctl", "--user", "daemon-reload"],
            vec!["systemctl", "--user", "enable", enable.as_str()],
            vec!["systemctl", "--user", "restart", unit],
        ] {
            if !self.sys.run(&argv)?.0 {
                return Err(format!("{} failed", argv.join(" ")));
            }
        }
        let (_, id) = self.sys.run(&[
            "systemctl",
            "--user",
            "show",
            "-p",
            "InvocationID",
            "--value",
            unit,
        ])?;
        Ok(Some(id.trim().to_owned()).filter(|s| !s.is_empty()))
    }

    pub fn read(&self, unit: &str) -> Result<RunRead, String> {
        if let Some(bin) = unit.strip_prefix("bin:") {
            let installed = match self.require_landed() {
                Ok(head) => {
                    let repo = self.repo.to_string_lossy().into_owned();
                    let mut argv = vec!["host-sync", "--check-release", &repo, &head];
                    argv.extend(self.release_flag(bin));
                    let (ok, out) = self.sys.run(&argv)?;
                    let receipt: serde_json::Value =
                        serde_json::from_str(out.trim()).unwrap_or_default();
                    ok && receipt["binaries"]
                        .as_object()
                        .is_some_and(|b| b.keys().any(|k| k.trim_end_matches(".exe") == bin))
                }
                Err(_) => false,
            };
            let is_active = if installed { "installed" } else { "stale" };
            return Ok(RunRead {
                is_active: is_active.into(),
                result: None,
                exec_main_exit_timestamp: None,
                oneshot: false,
                invocation: None,
            });
        }
        let (_, out) = self
            .sys
            .run(&["systemctl", "--user", "show", unit, "-p", SHOW])?;
        Ok(parse_show(&out))
    }

    pub fn disable(&self, unit: &str) -> Result<(), String> {
        if unit.starts_with("bin:") {
            return Ok(());
        }
        match self
            .sys
            .run(&["systemctl", "--user", "disable", "--now", unit])?
        {
            (true, _) => Ok(()),
            _ => Err(format!("systemctl --user disable --now {unit} failed")),
        }
    }
}

/// `land-train deploy-host`: one host step; `check` prints its `MISSING` lines.
pub(super) fn run_cli(flags: &BTreeMap<String, String>) -> Result<Vec<String>, String> {
    let need = |k: &str| {
        flags
            .get(k)
            .cloned()
            .ok_or_else(|| format!("deploy-host: missing --{k}"))
    };
    let step = Step {
        sys: &RealSys,
        repo: PathBuf::from(need("repo")?),
        rev: need("rev")?,
    };
    let unit = need("unit")?;
    match need("op")?.as_str() {
        "check" => step.check(&unit),
        "install" => Ok(step.install(&unit)?.into_iter().collect()),
        "read" => Ok(vec![
            serde_json::to_string(&step.read(&unit)?).map_err(|e| e.to_string())?
        ]),
        "disable" => step.disable(&unit).map(|()| vec![]),
        op => Err(format!(
            "deploy-host: unknown --op {op} (check, install, read, disable)"
        )),
    }
}
